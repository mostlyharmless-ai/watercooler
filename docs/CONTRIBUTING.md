# Contributing to Watercooler-Cloud

Thank you for your interest in contributing to watercooler-cloud! This document provides guidelines and information for contributors.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Setup](#development-setup)
- [How to Contribute](#how-to-contribute)
- [Pull Request Process](#pull-request-process)
- [Coding Standards](#coding-standards)
- [Testing Guidelines](#testing-guidelines)
- [Documentation](#documentation)
- [Community](#community)

---

## Code of Conduct

This project adheres to a code of conduct that all contributors are expected to follow:

- **Be respectful** - Value diverse perspectives and experiences
- **Be collaborative** - Work together constructively
- **Be professional** - Focus on technical merits
- **Be inclusive** - Welcome newcomers and help them succeed

Report any unacceptable behavior to the project maintainers.

---

## Getting Started

### Prerequisites

- **Python 3.10 or later** (required for watercooler-cloud)
- **Git** for version control
- **GitHub account** for submitting pull requests

### Quick Start

1. **Fork the repository** on GitHub
2. **Clone your fork:**
   ```bash
   git clone https://github.com/YOUR_USERNAME/watercooler-cloud.git
   cd watercooler-cloud
   ```
3. **Add upstream remote:**
   ```bash
   git remote add upstream https://github.com/mostlyharmless-ai/watercooler-cloud.git
   ```

---

## Development Setup

### Install in Development Mode

```bash
# Install with all development dependencies
pip install -e .[dev,mcp]

# Or if using conda/mamba
conda create -n watercooler python=3.10
conda activate watercooler
pip install -e .[dev,mcp]
```

**Development extras include:**
- `pytest` - Testing framework
- `pytest-cov` - Code coverage
- `black` - Code formatter
- `mypy` - Type checker
- `ruff` - Linter
- `fastmcp` - MCP server framework

### Verify Installation

```bash
# Test CLI
watercooler --help

# Test MCP server
python3 -m watercooler_mcp

# Run tests
pytest tests/
```

---

## How to Contribute

### Types of Contributions

We welcome various types of contributions:

#### Bug Reports
- Search existing issues first
- Include minimal reproduction steps
- Provide environment details (OS, Python version)
- Include error messages and stack traces

#### Feature Requests
- Check [ROADMAP.md](../ROADMAP.md) for planned features
- Describe the use case and expected behavior
- Explain why this would be valuable
- Consider if it fits the project's goals

#### Documentation
- Fix typos and clarify confusing sections
- Add examples and use cases
- Improve API documentation
- Update outdated information

#### Code Contributions
- Bug fixes
- New features (discuss first in an issue)
- Performance improvements
- Test coverage improvements

### Before Starting Work

1. **Check existing issues** - Someone may already be working on it
2. **Open an issue** - Discuss your approach before major changes
3. **Get feedback** - Especially for new features or breaking changes

---

## Pull Request Process

### 1. Create a Feature Branch

```bash
# Sync with upstream
git fetch upstream
git checkout main
git merge upstream/main

# Create feature branch
git checkout -b feature/your-feature-name
# or
git checkout -b fix/bug-description
```

**Branch naming conventions:**
- `feature/` - New features
- `fix/` - Bug fixes
- `docs/` - Documentation changes
- `refactor/` - Code refactoring
- `test/` - Test improvements

### 2. Make Your Changes

- **Write clear commit messages** (see [Commit Guidelines](#commit-guidelines))
- **Add tests** for new functionality
- **Update documentation** as needed
- **Run tests locally** before pushing

### 3. Commit Your Changes

```bash
# Stage your changes
git add .

# Commit with descriptive message
git commit -m "feat: add support for custom entry templates

- Allow users to override entry templates
- Add WATERCOOLER_ENTRY_TEMPLATE env var
- Update documentation with examples

Closes #123"
```

### 4. Push and Create Pull Request

```bash
# Push to your fork
git push origin feature/your-feature-name
```

Then open a pull request on GitHub:
1. Go to your fork on GitHub
2. Click "Pull Request"
3. Select `main` as the base branch
4. Fill in the PR template (see below)

### PR Template

```markdown
## Description
Brief description of what this PR does.

## Type of Change
- [ ] Bug fix (non-breaking change which fixes an issue)
- [ ] New feature (non-breaking change which adds functionality)
- [ ] Breaking change (fix or feature that would cause existing functionality to not work as expected)
- [ ] Documentation update

## Testing
- [ ] Tests pass locally
- [ ] New tests added for new functionality
- [ ] Documentation updated

## Related Issues
Closes #(issue number)

## Screenshots (if applicable)
```

### 5. Code Review Process

- **Maintainers will review** your PR
- **Address feedback** - Make requested changes
- **Keep PR focused** - One feature/fix per PR
- **Be patient** - Reviews may take time

---

## Coding Standards

### Python Style

We follow **PEP 8** with these tools:

```bash
# Format code
black src/ tests/

# Lint code
ruff check src/ tests/

# Type check
mypy src/
```

### Code Guidelines

#### 1. Type Hints

Always use type hints for function signatures:

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
    ...
```

#### 2. Docstrings

Use Google-style docstrings:

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
    ...
```

#### 3. Error Handling

- Use specific exception types
- Provide helpful error messages
- Include context in exceptions

```python
try:
    content = thread_path.read_text()
except FileNotFoundError:
    raise FileNotFoundError(
        f"Thread '{topic}' not found at {thread_path}"
    )
```

#### 4. Naming Conventions

- **Functions/methods**: `snake_case`
- **Classes**: `PascalCase`
- **Constants**: `UPPER_SNAKE_CASE`
- **Private**: `_leading_underscore`

---

## Testing Guidelines

### Running Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=watercooler --cov-report=html

# Run specific test file
pytest tests/test_commands.py

# Run specific test
pytest tests/test_commands.py::test_say_command
```

### Writing Tests

#### Test Structure

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

#### Test Coverage Goals

- **Core functionality**: 90%+ coverage
- **Edge cases**: Test error conditions
- **Integration**: Test CLI commands end-to-end

#### Test Fixtures

Use pytest fixtures for common setup:

```python
@pytest.fixture
def temp_threads_dir(tmp_path: Path) -> Path:
    """Create temporary threads directory."""
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    return threads_dir
```

---

## Documentation

### Documentation Standards

- **Clear and concise** - Avoid jargon
- **Examples** - Show don't just tell
- **Up-to-date** - Update docs with code changes
- **Cross-references** - Link to related docs

### Documentation Types

#### 1. Code Documentation

- **Docstrings** - All public functions/classes
- **Type hints** - All function signatures
- **Comments** - For complex logic only

#### 2. User Documentation

Update these files as needed:
- `docs/QUICKSTART.md` - Getting started guide
- `docs/archive/integration.md` - Integration patterns
- `docs/mcp-server.md` - MCP tool reference
- `docs/TROUBLESHOOTING.md` - Common issues

#### 3. API Documentation

- `docs/archive/integration.md#python-api-reference` - Python API reference
- Keep examples current
- Document all public APIs

### Documentation Workflow

1. **Make code changes**
2. **Update relevant docs** in the same PR
3. **Add examples** if introducing new features
4. **Update ROADMAP.md** if changing project status

---

## Commit Guidelines

### Commit Message Format

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

---

## Branch Strategy

We use a three-branch model for development and deployment:

```
feature/* --PR--> main (development) --PR--> staging (release candidate) --FF--> stable + tag
                                                                                    |
                                                                              v0.1.2, v0.2.0
```

### Branch Purposes

| Branch | Purpose | How Code Arrives | Testing |
|--------|---------|------------------|---------|
| `feature/*` | Work in progress | Direct push | PR CI |
| `main` | Integrated development | PR from feature branches | CI on merge |
| `staging` | Release candidate | PR from main | CI on merge |
| `stable` | Production releases | Fast-forward from staging | No tests (already validated) |

### What Each Branch Represents

- **`main`**: Latest development work. May have rough edges. Version is always `X.Y.Z-dev`.
- **`staging`**: Code being prepared for release. Version is `X.Y.Z` (no `-dev`). This is the **release candidate**.
- **`stable`**: Production-ready code that users install from. Always matches a tagged release.

### Which Branch Should Users Install From?

```bash
# Production (recommended) - always a tagged release
uvx --from "git+https://github.com/mostlyharmless-ai/watercooler-cloud@stable"

# Pinned version (check releases for available tags)
uvx --from "git+https://github.com/mostlyharmless-ai/watercooler-cloud@v0.1.2"

# Bleeding edge (developers only)
uvx --from "git+https://github.com/mostlyharmless-ai/watercooler-cloud@main"
```

### Branch Protection

| Branch | PR Required | Approvals | CI Must Pass | Push Access |
|--------|-------------|-----------|--------------|-------------|
| `main` | Yes | 0 | Yes | Via PR only |
| `staging` | Yes | 0 | Yes | Via PR only |
| `stable` | No | - | No | Maintainers only (FF) |

**Why 0 approvals on main/staging:**
- For small teams, CI is the primary quality gate
- Requiring approvals can create bottlenecks
- Code review happens informally or async
- CI ensures nothing broken gets merged

**Why `stable` doesn't require PRs or CI:**
- Code is already reviewed and tested when it reaches staging
- The staging -> stable transition is a fast-forward merge only
- Running tests again would be redundant
- This is a mechanical release step, not a review step
- The tag triggers the release workflow

---

## Versioning

### Single-Source Version

The version is defined in **one place only**: `pyproject.toml`

```toml
[project]
version = "0.1.3-dev"
```

The `__init__.py` reads this dynamically via `importlib.metadata`:

```python
from importlib.metadata import version
__version__ = version("watercooler-cloud")
```

**Never edit version anywhere else** - it's automatically derived from `pyproject.toml`.

### Version Lifecycle

```
v0.1.1 released on stable
         |
         v
main: "0.1.2-dev"     <-- development happens here
         |
         | (bump version for release PR)
         v
main: "0.1.2"         <-- version bump commit
         |
         | (PR: main -> staging)
         v
staging: "0.1.2"      <-- release candidate validated
         |
         | (FF merge + tag)
         v
stable: "0.1.2"       <-- released! (tag: v0.1.2)
         |
         | (bump to next dev version)
         v
main: "0.1.3-dev"     <-- ready for next cycle
```

### Semantic Versioning

We follow **SemVer** (`MAJOR.MINOR.PATCH`):
- **MAJOR** - Breaking API changes
- **MINOR** - New features (backward compatible)
- **PATCH** - Bug fixes

Development versions use `-dev` suffix: `0.1.3-dev`

---

## Release Process

### Overview

The release process has **5 phases**:

1. **Development** - Features merged to main via PR
2. **Prepare** - Bump version, create release PR to staging
3. **Validate** - CI runs on staging PR, human review
4. **Release** - Fast-forward stable, create tag
5. **Post-release** - Bump main to next dev version

### Phase 1: Development (feature -> main)

```bash
# Create feature branch
git checkout -b feature/my-feature main

# Work, commit, push
git commit -m "feat: add new feature"
git push origin feature/my-feature

# Create PR to main
gh pr create --base main --title "Add new feature"
```

**Requirements:**
- PR required
- CI must pass
- Code review (human or automated)

### Phase 2: Prepare Release (version bump)

When ready to release, bump the version on main:

```bash
git checkout main
git pull origin main

# Edit pyproject.toml: "0.1.2-dev" -> "0.1.2"
# (Remove the -dev suffix)

git commit -m "chore(release): prepare v0.1.2"
git push origin main
```

### Phase 3: Create Release PR (main -> staging)

```bash
# Create PR from main to staging
gh pr create --base staging --head main --title "Release v0.1.2"
```

**Requirements:**
- PR required
- CI must pass (this is the release validation)
- Human review: "Is this ready for production?"

After approval, merge the PR (via GitHub UI).

### Phase 4: Release to Production (staging -> stable)

```bash
git fetch origin
git checkout stable
git merge --ff-only origin/staging

# Create annotated tag
git tag -a v0.1.2 -m "Release v0.1.2 - Brief description"

# Push branch and tag
git push origin stable --tags
```

**What happens:**
- Tag push triggers release workflow
- GitHub Release page is created automatically
- No tests run (already validated on staging)

### Phase 5: Post-Release (bump dev version)

```bash
git checkout main

# Edit pyproject.toml: "0.1.2" -> "0.1.3-dev"

git commit -m "chore: bump version to 0.1.3-dev"
git push origin main
```

### Release Checklist

```markdown
## Pre-Release
- [ ] All features for this release are merged to main
- [ ] CI passing on main

## Prepare
- [ ] Bump version in pyproject.toml (remove -dev)
- [ ] Commit: "chore(release): prepare vX.Y.Z"
- [ ] Create PR: main -> staging

## Validate
- [ ] CI passing on staging PR
- [ ] Review and approve PR
- [ ] Merge PR to staging

## Release
- [ ] git checkout stable
- [ ] git merge --ff-only origin/staging
- [ ] git tag -a vX.Y.Z -m "Release vX.Y.Z - description"
- [ ] git push origin stable --tags
- [ ] Verify GitHub Release was created

## Post-Release
- [ ] Bump main to next dev version (X.Y.Z+1-dev)
- [ ] Commit: "chore: bump version to X.Y.Z+1-dev"
```

### Tags Explained

**What are tags?**
- Immutable pointers to specific commits
- Used for versioning releases
- Users install from tags for reproducibility

**Important: Tags must be created on `stable` branch only.**
The release workflow triggers on any `v*` tag push. Creating a tag from the wrong branch (e.g., `main` or a feature branch) will create a broken release.

**Tag naming:**
- `v0.1.2` - Production release
- `v0.1.2-rc1` - Release candidate (optional)
- `v0.1.2-beta` - Pre-release/beta

**Prerelease detection:**
The release workflow automatically marks tags containing `-` as prereleases:
- `v0.1.2` -> full release
- `v0.1.2-beta` -> prerelease

### Rollback Procedure

If a release has critical issues:

**Option 1: Release a patch (recommended)**
```bash
# Fix the bug on main, then follow normal release process
# This creates v0.1.3 which supersedes the broken v0.1.2
```

**Option 2: Delete bad release and re-release (minor issues)**
```bash
# Delete the GitHub release via web UI or:
gh release delete v0.1.2 --yes

# Delete the bad tag
git tag -d v0.1.2
git push origin --delete v0.1.2

# Fix the issue, then re-tag and re-release
```

**Option 3: Revert stable branch (emergency only, requires admin)**
```bash
# NOTE: Force-push is blocked by branch protection.
# An admin must temporarily disable protection, then re-enable after.

git checkout stable
git reset --hard v0.1.1
git push --force-with-lease origin stable  # Requires admin override
```

> **Warning**: Force-pushing to `stable` affects users with cached installations (they may need to clear uvx cache). Only use for critical security issues. Prefer releasing a patch version instead.

---

## Community

### Getting Help

- **Documentation**: Start with [README.md](../README.md) and [docs/](.)
- **Issues**: Search existing issues on GitHub
- **Discussions**: Use GitHub Discussions for questions

### Reporting Issues

**Bug reports should include:**
1. **Description** - What happened vs. what you expected
2. **Reproduction** - Minimal steps to reproduce
3. **Environment** - OS, Python version, watercooler version
4. **Logs** - Error messages and stack traces

**Use the issue template:**

```markdown
## Bug Description
Clear description of the bug

## Steps to Reproduce
1. Step one
2. Step two
3. See error

## Expected Behavior
What you expected to happen

## Environment
- OS: macOS 14.0
- Python: 3.11.5
- Watercooler: 0.2.0
- MCP Client: Claude Code

## Additional Context
Any other relevant information
```

### Feature Requests

**Feature requests should include:**
1. **Use case** - What problem does this solve?
2. **Expected behavior** - How should it work?
3. **Alternatives** - Other solutions considered
4. **Scope** - Is this a core feature or extension?

---

## Project Structure

Understanding the codebase:

```
watercooler-cloud/
├── src/
│   ├── watercooler/              # Core library
│   │   ├── __init__.py
│   │   ├── agents.py             # Agent registry and canonicalization
│   │   ├── baseline_graph/       # Baseline graph JSONL export (package)
│   │   ├── cli.py                # CLI entry point
│   │   ├── commands.py           # High-level command implementations
│   │   ├── commands_graph.py     # Graph-related CLI commands
│   │   ├── config_facade.py      # Unified configuration facade
│   │   ├── config_loader.py      # TOML loading and merging
│   │   ├── config_schema.py      # Pydantic configuration models
│   │   ├── constants.py          # Shared constants
│   │   ├── credentials.py        # Credential management
│   │   ├── fs.py                 # File system operations
│   │   ├── lock.py               # Advisory file locking
│   │   ├── memory_config.py      # Memory backend configuration
│   │   ├── models.py             # Shared data models
│   │   ├── path_resolver.py      # Git-aware path resolution
│   │   ├── schema_validation.py  # Schema validation utilities
│   │   ├── slack_cli.py          # Slack CLI commands
│   │   ├── templates/            # Built-in templates
│   │   ├── templates.py          # Template loading
│   │   ├── testing.py            # Test utilities (context managers, fixtures)
│   │   └── thread_entries.py     # Thread entry operations
│   └── watercooler_mcp/          # MCP server
│       ├── __init__.py
│       ├── __main__.py           # python -m entry point
│       ├── auth.py               # Authentication
│       ├── cache.py              # Caching layer
│       ├── config.py             # MCP-specific configuration
│       ├── context.py            # Request context
│       ├── errors.py             # Error types
│       ├── github_api.py         # GitHub API integration
│       ├── helpers.py            # Shared helpers
│       ├── hosted_ops.py         # Hosted operation handlers
│       ├── memory.py             # Memory backend integration
│       ├── memory_queue/         # Persistent memory task queue
│       │   ├── checkpoint.py     # Queue checkpointing
│       │   ├── errors.py         # Queue error types
│       │   ├── queue.py          # Queue implementation
│       │   ├── task.py           # Task definitions
│       │   └── worker.py         # Background worker
│       ├── memory_sync.py        # Memory synchronization
│       ├── middleware.py         # Request middleware
│       ├── observability.py      # Logging and monitoring
│       ├── repo_setup.py         # Repository setup
│       ├── resources.py          # MCP resources
│       ├── server.py             # FastMCP server implementation
│       ├── server_http.py        # HTTP transport server
│       ├── startup.py            # Server startup logic
│       ├── sync/                 # Git sync primitives and locking
│       │   ├── __init__.py       # Per-topic advisory locks, sanitization
│       │   ├── errors.py         # Rich exception hierarchy
│       │   └── primitives.py     # Pure git operations
│       ├── tools/                # MCP tool implementations
│       │   ├── diagnostic.py     # Diagnostic tools
│       │   ├── graph.py          # Graph tools
│       │   ├── memory.py         # Memory tools
│       │   ├── migration.py      # Migration tools
│       │   ├── sync.py           # Sync tools
│       │   ├── thread_query.py   # Thread query tools
│       │   └── thread_write.py   # Thread write tools
│       ├── validation.py         # Input validation
│       └── slack/                # Slack integration (reference)
├── tests/                        # Test suite
│   ├── test_commands.py
│   ├── test_cli.py
│   └── ...
├── docs/                         # Documentation
│   ├── QUICKSTART.md
│   ├── CONTRIBUTING.md
│   └── ...
└── ROADMAP.md                    # Project status and roadmap
```

---

## License

By contributing, you agree that your contributions will be licensed under the same license as the project.

---

## Questions?

- **Documentation**: Check [docs/](.)
- **Issues**: [GitHub Issues](https://github.com/mostlyharmless-ai/watercooler-cloud/issues)
- **Discussions**: [GitHub Discussions](https://github.com/mostlyharmless-ai/watercooler-cloud/discussions)

Thank you for contributing to watercooler-cloud!
