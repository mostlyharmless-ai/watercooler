"""Smoke tests for L0 phase - verify basic package structure."""

import re


def test_import():
    """Verify package can be imported and has valid semver version."""
    import watercooler
    # Version should match semver pattern (e.g., 0.1.0, 1.2.3-dev)
    assert re.match(r"^\d+\.\d+\.\d+", watercooler.__version__)


def test_version_string():
    """Verify version is a string."""
    import watercooler
    assert isinstance(watercooler.__version__, str)
    assert len(watercooler.__version__) > 0


def test_version_consistency():
    """Verify watercooler and watercooler_mcp have same version."""
    import watercooler
    import watercooler_mcp
    assert watercooler.__version__ == watercooler_mcp.__version__
