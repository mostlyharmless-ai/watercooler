"""Tests for branch sync enforcement operations."""
from __future__ import annotations

import pytest
from pathlib import Path
from git import Repo, Actor
from watercooler.commands import check_branches, check_branch, merge_branch, archive_branch
from watercooler_mcp.git_sync import validate_branch_pairing


@pytest.fixture
def code_repo(tmp_path: Path) -> Path:
    """Create a temporary code repository."""
    repo_path = tmp_path / "code-repo"
    repo_path.mkdir()
    repo = Repo.init(repo_path)
    
    # Create initial commit
    (repo_path / "README.md").write_text("# Code Repo\n")
    repo.index.add(["README.md"])
    repo.index.commit("Initial commit", author=Actor("Test", "test@example.com"))
    
    # Create main branch
    repo.git.checkout("-b", "main")
    
    return repo_path


@pytest.fixture
def threads_repo(tmp_path: Path) -> Path:
    """Create a temporary threads repository."""
    repo_path = tmp_path / "code-repo-threads"
    repo_path.mkdir()
    repo = Repo.init(repo_path)
    
    # Create initial commit
    (repo_path / "README.md").write_text("# Threads Repo\n")
    repo.index.add(["README.md"])
    repo.index.commit("Initial commit", author=Actor("Test", "test@example.com"))
    
    # Create main branch
    repo.git.checkout("-b", "main")
    
    return repo_path


def test_validate_branch_pairing_synced(code_repo: Path, threads_repo: Path) -> None:
    """Test validation when branches are properly synced."""
    result = validate_branch_pairing(code_repo, threads_repo, strict=True)
    assert result.valid
    assert result.code_branch == "main"
    assert result.threads_branch == "main"
    assert len(result.mismatches) == 0


def test_validate_branch_pairing_mismatch(code_repo: Path, threads_repo: Path) -> None:
    """Test validation when branches don't match."""
    code_repo_obj = Repo(code_repo)
    threads_repo_obj = Repo(threads_repo)
    
    # Create feature branch in code repo
    code_repo_obj.git.checkout("-b", "feature-auth")
    
    # Threads repo stays on main
    result = validate_branch_pairing(code_repo, threads_repo, strict=True)
    assert not result.valid
    assert result.code_branch == "feature-auth"
    assert result.threads_branch == "main"
    assert len(result.mismatches) > 0
    assert any(m.type == "branch_name_mismatch" for m in result.mismatches)


def test_check_branches_synced(code_repo: Path, threads_repo: Path) -> None:
    """Test check_branches when branches are synced."""
    result = check_branches(code_root=code_repo)
    assert "Synchronized Branches" in result
    assert "main" in result
    assert "Drift Detected" not in result


def test_check_branches_drift(code_repo: Path, threads_repo: Path) -> None:
    """Test check_branches when there's drift."""
    code_repo_obj = Repo(code_repo)
    threads_repo_obj = Repo(threads_repo)
    
    # Create feature branch in code repo only
    code_repo_obj.git.checkout("-b", "feature-auth")
    (code_repo / "feature.txt").write_text("feature")
    code_repo_obj.index.add(["feature.txt"])
    code_repo_obj.index.commit("Add feature", author=Actor("Test", "test@example.com"))
    
    # Create orphaned branch in threads repo
    threads_repo_obj.git.checkout("-b", "orphaned-branch")
    (threads_repo / "orphan.md").write_text("# Orphan")
    threads_repo_obj.index.add(["orphan.md"])
    threads_repo_obj.index.commit("Orphan thread", author=Actor("Test", "test@example.com"))
    threads_repo_obj.git.checkout("main")
    
    result = check_branches(code_root=code_repo)
    assert "Drift Detected" in result
    assert "feature-auth" in result
    assert "orphaned-branch" in result


def test_merge_branch_with_open_threads(code_repo: Path, threads_repo: Path) -> None:
    """Test merge_branch warns about OPEN threads."""
    threads_repo_obj = Repo(threads_repo)
    
    # Create feature branch with OPEN thread
    threads_repo_obj.git.checkout("-b", "feature-auth")
    (threads_repo / "auth-implementation.md").write_text(
        "# auth-implementation\n\nStatus: OPEN\nBall: Test\n\n---\n"
    )
    threads_repo_obj.index.add(["auth-implementation.md"])
    threads_repo_obj.index.commit("Add thread", author=Actor("Test", "test@example.com"))
    
    result = merge_branch("feature-auth", code_root=code_repo, force=False)
    assert "OPEN threads" in result
    assert "auth-implementation" in result
    assert "Warning" in result or "⚠️" in result


def test_merge_branch_force(code_repo: Path, threads_repo: Path) -> None:
    """Test merge_branch with force flag bypasses warnings."""
    threads_repo_obj = Repo(threads_repo)
    
    # Create feature branch
    threads_repo_obj.git.checkout("-b", "feature-auth")
    (threads_repo / "auth.md").write_text("# auth\n\nStatus: CLOSED\n\n---\n")
    threads_repo_obj.index.add(["auth.md"])
    threads_repo_obj.index.commit("Add thread", author=Actor("Test", "test@example.com"))
    
    result = merge_branch("feature-auth", code_root=code_repo, force=True)
    assert "Merged" in result or "✅" in result
    
    # Verify merge happened
    threads_repo_obj.git.checkout("main")
    assert "feature-auth" in [b.name for b in threads_repo_obj.heads]


def test_archive_branch_with_open_threads(code_repo: Path, threads_repo: Path) -> None:
    """Test archive_branch closes OPEN threads."""
    threads_repo_obj = Repo(threads_repo)
    
    # Create feature branch with OPEN thread
    threads_repo_obj.git.checkout("-b", "feature-auth")
    (threads_repo / "auth.md").write_text(
        "# auth\n\nStatus: OPEN\nBall: Test\n\n---\n"
    )
    threads_repo_obj.index.add(["auth.md"])
    threads_repo_obj.index.commit("Add thread", author=Actor("Test", "test@example.com"))
    
    result = archive_branch("feature-auth", code_root=code_repo, abandon=False, force=True)
    assert "Archived" in result or "✅" in result
    
    # Verify thread was closed
    threads_repo_obj.git.checkout("main")
    thread_content = (threads_repo / "auth.md").read_text()
    assert "CLOSED" in thread_content.upper()
    
    # Verify branch was deleted
    assert "feature-auth" not in [b.name for b in threads_repo_obj.heads]


def test_archive_branch_abandon(code_repo: Path, threads_repo: Path) -> None:
    """Test archive_branch with abandon flag sets ABANDONED status."""
    threads_repo_obj = Repo(threads_repo)
    
    # Create feature branch with OPEN thread
    threads_repo_obj.git.checkout("-b", "feature-auth")
    (threads_repo / "auth.md").write_text(
        "# auth\n\nStatus: OPEN\nBall: Test\n\n---\n"
    )
    threads_repo_obj.index.add(["auth.md"])
    threads_repo_obj.index.commit("Add thread", author=Actor("Test", "test@example.com"))
    
    result = archive_branch("feature-auth", code_root=code_repo, abandon=True, force=True)
    assert "ABANDONED" in result
    
    # Verify thread was set to ABANDONED
    threads_repo_obj.git.checkout("main")
    thread_content = (threads_repo / "auth.md").read_text()
    assert "ABANDONED" in thread_content.upper()

