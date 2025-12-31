import os
import shutil
import subprocess
from pathlib import Path

import pytest

from watercooler_mcp.config import resolve_thread_context


def _git_available() -> bool:
    return shutil.which("git") is not None


def test_resolve_thread_context_explicit_dir(tmp_path, monkeypatch):
    explicit_dir = tmp_path / ".watercooler"
    monkeypatch.setenv("WATERCOOLER_DIR", str(explicit_dir))
    monkeypatch.delenv("WATERCOOLER_THREADS_BASE", raising=False)
    monkeypatch.delenv("WATERCOOLER_THREADS_PATTERN", raising=False)
    monkeypatch.delenv("WATERCOOLER_GIT_REPO", raising=False)
    monkeypatch.delenv("WATERCOOLER_CODE_REPO", raising=False)

    context = resolve_thread_context()

    assert context.explicit_dir is True
    assert context.threads_dir == explicit_dir
    assert context.threads_repo_url is None


@pytest.mark.skipif(not _git_available(), reason="git not available in test environment")
def test_resolve_thread_context_git_pattern(tmp_path, monkeypatch):
    repo_dir = tmp_path / "code"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/mostly/test.git"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    # Create initial commit to establish a branch
    (repo_dir / "README.md").write_text("test")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )

    threads_base = tmp_path / "threads"
    monkeypatch.delenv("WATERCOOLER_DIR", raising=False)
    monkeypatch.setenv("WATERCOOLER_THREADS_BASE", str(threads_base))
    monkeypatch.delenv("WATERCOOLER_GIT_REPO", raising=False)
    monkeypatch.delenv("WATERCOOLER_CODE_REPO", raising=False)

    context = resolve_thread_context(repo_dir)

    expected_dir = (threads_base / "mostly" / "test-threads").resolve()
    assert context.threads_dir == expected_dir
    # Default URL pattern is HTTPS (works without SSH agent)
    assert context.threads_repo_url == "https://github.com/mostly/test-threads.git"
    assert context.code_repo == "mostly/test"
    assert context.code_branch in {"master", "main"}


@pytest.mark.skipif(not _git_available(), reason="git not available in test environment")
def test_resolve_thread_context_infers_repo_from_code_remote(tmp_path, monkeypatch):
    repo_dir = tmp_path / "code"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True, capture_output=True)
    (repo_dir / "README.md").write_text("test")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/acme/example.git"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )

    monkeypatch.delenv("WATERCOOLER_DIR", raising=False)
    monkeypatch.delenv("WATERCOOLER_THREADS_BASE", raising=False)
    monkeypatch.delenv("WATERCOOLER_THREADS_PATTERN", raising=False)
    monkeypatch.delenv("WATERCOOLER_GIT_REPO", raising=False)
    monkeypatch.delenv("WATERCOOLER_CODE_REPO", raising=False)

    context = resolve_thread_context(repo_dir)

    # Derived URL should mirror the code remote host with -threads suffix
    assert context.threads_repo_url.endswith("acme/example-threads.git")
    assert context.threads_slug == "acme/example-threads"


@pytest.mark.skipif(not _git_available(), reason="git not available in test environment")
def test_ssh_url_uses_https_when_agent_unavailable(tmp_path, monkeypatch):
    """Test HTTPS fallback when SSH_AUTH_SOCK is unavailable (Codex compatibility)."""
    # Set up a git repo with SSH remote
    repo_dir = tmp_path / "code"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True, capture_output=True)
    (repo_dir / "README.md").write_text("test")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial"], cwd=repo_dir, check=True, capture_output=True)
    # Use SSH remote
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:acme/ssh-test.git"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )

    # Clear all relevant env vars including SSH_AUTH_SOCK
    monkeypatch.delenv("WATERCOOLER_DIR", raising=False)
    monkeypatch.delenv("WATERCOOLER_THREADS_BASE", raising=False)
    monkeypatch.delenv("WATERCOOLER_THREADS_PATTERN", raising=False)
    monkeypatch.delenv("WATERCOOLER_GIT_REPO", raising=False)
    monkeypatch.delenv("WATERCOOLER_CODE_REPO", raising=False)
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)

    context = resolve_thread_context(repo_dir)

    # Without SSH_AUTH_SOCK, should use HTTPS pattern (default config)
    # This prevents Codex MCP hangs when SSH agent is unavailable
    assert context.threads_repo_url.startswith("https://")


@pytest.mark.skipif(not _git_available(), reason="git not available in test environment")
def test_ssh_url_uses_ssh_when_agent_available(tmp_path, monkeypatch):
    """Test SSH is used when SSH_AUTH_SOCK is available and config specifies SSH."""
    # Set up a git repo with SSH remote
    repo_dir = tmp_path / "code"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True, capture_output=True)
    (repo_dir / "README.md").write_text("test")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:acme/ssh-test.git"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )

    # Clear relevant env vars but SET SSH_AUTH_SOCK
    monkeypatch.delenv("WATERCOOLER_DIR", raising=False)
    monkeypatch.delenv("WATERCOOLER_THREADS_BASE", raising=False)
    monkeypatch.delenv("WATERCOOLER_GIT_REPO", raising=False)
    monkeypatch.delenv("WATERCOOLER_CODE_REPO", raising=False)
    # Explicitly set SSH pattern to test the fallback logic (not default config)
    monkeypatch.setenv("WATERCOOLER_THREADS_PATTERN", "git@github.com:{org}/{repo}-threads.git")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-agent.sock")

    context = resolve_thread_context(repo_dir)

    # With SSH_AUTH_SOCK and explicit SSH pattern, should use SSH
    assert context.threads_repo_url.startswith("git@")
