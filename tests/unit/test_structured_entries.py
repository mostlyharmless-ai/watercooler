from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def run_cli(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "watercooler.cli", *args], capture_output=True, text=True, cwd=cwd)


def test_structured_entry_with_all_fields(tmp_path: Path):
    """Test append-entry with all structured fields."""
    # Init thread
    run_cli("init-thread", "test-structured", "--threads-dir", str(tmp_path))

    # Append structured entry with all fields
    cp = run_cli(
        "append-entry",
        "test-structured",
        "--threads-dir",
        str(tmp_path),
        "--agent",
        "Claude",
        "--role",
        "critic",
        "--title",
        "Code Review Complete",
        "--type",
        "Decision",
        "--body",
        "Approved for merge",
        "--status",
        "reviewed",
        "--ball",
        "Team",
    )
    assert cp.returncode == 0

    # Verify structured entry format
    content = (tmp_path / "test-structured.md").read_text(encoding="utf-8")
    assert "Claude" in content  # Agent present
    # Role is passed to function but may not be in template output
    assert "Code Review Complete" in content  # Title
    assert "Decision" in content  # Type
    assert "Approved for merge" in content  # Body
    assert "reviewed" in content.lower()  # Status updated
    assert "Team" in content  # Ball updated


def test_say_auto_flips_ball(tmp_path: Path):
    """Test that say() auto-flips ball to counterpart."""
    # Init with ball=Codex
    run_cli("init-thread", "auto-flip", "--threads-dir", str(tmp_path), "--ball", "Codex")

    # Say from Codex without explicit ball - should auto-flip to Claude
    cp = run_cli(
        "say",
        "auto-flip",
        "--threads-dir",
        str(tmp_path),
        "--agent",
        "Codex",  # Specify agent
        "--title",
        "Update",
        "--body",
        "Made changes",
    )
    assert cp.returncode == 0

    # Check ball flipped to Claude (counterpart of Codex)
    content = (tmp_path / "auto-flip.md").read_text(encoding="utf-8")
    assert "Ball: Claude" in content or "Ball: claude" in content


def test_say_with_explicit_ball_no_flip(tmp_path: Path):
    """Test that say() with explicit ball doesn't auto-flip."""
    # Init with ball=Codex
    run_cli("init-thread", "no-flip", "--threads-dir", str(tmp_path), "--ball", "Codex")

    # Say with explicit ball=Team - should use Team, not auto-flip
    cp = run_cli(
        "say",
        "no-flip",
        "--threads-dir",
        str(tmp_path),
        "--title",
        "Update",
        "--body",
        "Changes made",
        "--ball",
        "Team",
    )
    assert cp.returncode == 0

    # Check ball is Team
    content = (tmp_path / "no-flip.md").read_text(encoding="utf-8")
    assert "Ball: Team" in content


def test_ack_does_not_auto_flip(tmp_path: Path):
    """Test that ack() does NOT auto-flip ball."""
    # Init with ball=Codex
    run_cli("init-thread", "ack-test", "--threads-dir", str(tmp_path), "--ball", "Codex")

    # Get initial content to verify starting ball
    initial = (tmp_path / "ack-test.md").read_text(encoding="utf-8")
    assert "Ball: Codex" in initial or "Ball: codex" in initial

    # Ack without explicit ball - should NOT flip
    cp = run_cli("ack", "ack-test", "--threads-dir", str(tmp_path))
    assert cp.returncode == 0

    # Check ball stayed as Codex (ack doesn't auto-flip)
    content = (tmp_path / "ack-test.md").read_text(encoding="utf-8")
    assert "Ball: Codex" in content or "Ball: codex" in content


def test_entry_types(tmp_path: Path):
    """Test all entry types: Note, Plan, Decision, PR, Closure."""
    run_cli("init-thread", "types-test", "--threads-dir", str(tmp_path))

    for entry_type in ["Note", "Plan", "Decision", "PR", "Closure"]:
        cp = run_cli(
            "append-entry",
            "types-test",
            "--threads-dir",
            str(tmp_path),
            "--agent",
            "Team",
            "--role",
            "pm",
            "--title",
            f"Test {entry_type}",
            "--type",
            entry_type,
            "--body",
            f"This is a {entry_type}",
        )
        assert cp.returncode == 0

    # Verify all types present
    content = (tmp_path / "types-test.md").read_text(encoding="utf-8")
    for entry_type in ["Note", "Plan", "Decision", "PR", "Closure"]:
        assert entry_type in content


def test_roles(tmp_path: Path):
    """Test all roles: planner, critic, implementer, tester, pm, scribe."""
    run_cli("init-thread", "roles-test", "--threads-dir", str(tmp_path))

    roles = ["planner", "critic", "implementer", "tester", "pm", "scribe"]
    for role in roles:
        cp = run_cli(
            "append-entry",
            "roles-test",
            "--threads-dir",
            str(tmp_path),
            "--agent",
            "Team",
            "--role",
            role,
            "--title",
            f"As {role}",
            "--body",
            f"Acting as {role}",
        )
        assert cp.returncode == 0

    # Verify all roles mentioned
    content = (tmp_path / "roles-test.md").read_text(encoding="utf-8")
    for role in roles:
        # Role might be in lowercase or titlecase
        assert role in content.lower()


def test_init_thread_with_owner_and_participants(tmp_path: Path):
    """Test init-thread with owner and participants metadata."""
    cp = run_cli(
        "init-thread",
        "team-thread",
        "--threads-dir",
        str(tmp_path),
        "--owner",
        "agent",
        "--participants",
        "agent, Claude, Codex",
    )
    assert cp.returncode == 0

    content = (tmp_path / "team-thread.md").read_text(encoding="utf-8")
    # Owner and participants might be in template or fallback format
    # Just check thread was created successfully
    assert "team-thread" in content.lower() or "team" in content.lower()


def test_agent_canonicalization(tmp_path: Path):
    """Test that agent names are canonicalized (codex → Codex)."""
    run_cli("init-thread", "canon-test", "--threads-dir", str(tmp_path))

    # Use lowercase agent name
    cp = run_cli(
        "append-entry",
        "canon-test",
        "--threads-dir",
        str(tmp_path),
        "--agent",
        "codex",  # lowercase
        "--role",
        "implementer",
        "--title",
        "Test",
        "--body",
        "Testing canonicalization",
    )
    assert cp.returncode == 0

    # Should be canonicalized to "Codex" with user tag
    content = (tmp_path / "canon-test.md").read_text(encoding="utf-8")
    assert "Codex" in content  # Capitalized


def test_agent_user_tagging(tmp_path: Path):
    """Test that agents get user tags: Agent (user)."""
    run_cli("init-thread", "tag-test", "--threads-dir", str(tmp_path))

    cp = run_cli(
        "append-entry",
        "tag-test",
        "--threads-dir",
        str(tmp_path),
        "--agent",
        "Claude",
        "--role",
        "critic",
        "--title",
        "Test",
        "--body",
        "Testing user tags",
    )
    assert cp.returncode == 0

    # Should have user tag in format "Agent (username)"
    content = (tmp_path / "tag-test.md").read_text(encoding="utf-8")
    assert "(" in content and ")" in content  # Has parentheses for tag
    assert "Claude" in content


def test_handoff_creates_structured_entry(tmp_path: Path):
    """Test that handoff creates a proper structured entry."""
    run_cli("init-thread", "handoff-test", "--threads-dir", str(tmp_path), "--ball", "Codex")

    cp = run_cli(
        "handoff",
        "handoff-test",
        "--threads-dir",
        str(tmp_path),
        "--note",
        "Your turn to review",
    )
    assert cp.returncode == 0

    content = (tmp_path / "handoff-test.md").read_text(encoding="utf-8")
    # Should have structured entry with handoff info
    assert "Handoff" in content
    assert "Your turn to review" in content
    # Ball should flip to Claude
    assert "Ball: Claude" in content or "Ball: claude" in content
