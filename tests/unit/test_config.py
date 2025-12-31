from __future__ import annotations

import os
from pathlib import Path
from watercooler.path_resolver import resolve_templates_dir, load_template


def test_resolve_templates_dir_cli_arg():
    """CLI argument takes highest precedence."""
    result = resolve_templates_dir("/custom/path")
    assert result == Path("/custom/path")


def test_resolve_templates_dir_env_var(monkeypatch):
    """Environment variable takes precedence when no CLI arg."""
    monkeypatch.setenv("WATERCOOLER_TEMPLATES", "/env/path")
    result = resolve_templates_dir()
    assert result == Path("/env/path")


def test_resolve_templates_dir_project_local(tmp_path, monkeypatch):
    """Project-local .watercooler/templates/ takes precedence when exists."""
    # Remove env var if set
    monkeypatch.delenv("WATERCOOLER_TEMPLATES", raising=False)

    # Create project-local .watercooler/templates dir
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    watercooler_dir = project_dir / ".watercooler" / "templates"
    watercooler_dir.mkdir(parents=True)

    # Change to project dir
    original_cwd = os.getcwd()
    try:
        os.chdir(project_dir)
        result = resolve_templates_dir()
        # Should return absolute path to .watercooler/templates
        assert result.is_absolute()
        assert result == watercooler_dir.resolve()
    finally:
        os.chdir(original_cwd)


def test_resolve_templates_dir_package_default(tmp_path, monkeypatch):
    """Package bundled templates used as final fallback."""
    # Remove env var
    monkeypatch.delenv("WATERCOOLER_TEMPLATES", raising=False)

    # Change to dir without watercooler/ subdirectory
    original_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        result = resolve_templates_dir()
        # Should return package path (contains 'watercooler' and 'templates')
        assert "watercooler" in str(result).lower()
        assert "templates" in str(result).lower()
    finally:
        os.chdir(original_cwd)


def test_load_template_success():
    """Load bundled template successfully."""
    # Should be able to load bundled templates
    template = load_template("_TEMPLATE_topic_thread.md")
    assert "{{TOPIC}}" in template or "<TOPIC>" in template
    assert "{{BALL}}" in template or "<BALL>" in template
    assert "{{STATUS}}" in template or "<STATUS>" in template


def test_load_template_from_custom_dir(tmp_path):
    """Load template from custom directory."""
    custom_dir = tmp_path / "templates"
    custom_dir.mkdir()
    custom_template = custom_dir / "custom.md"
    custom_template.write_text("Custom: {{VALUE}}", encoding="utf-8")

    result = load_template("custom.md", custom_dir)
    assert result == "Custom: {{VALUE}}"


def test_load_template_not_found_raises():
    """FileNotFoundError when template doesn't exist."""
    try:
        load_template("nonexistent_template_xyz.md", Path("/tmp"))
        assert False, "Should have raised FileNotFoundError"
    except FileNotFoundError:
        pass  # Expected
