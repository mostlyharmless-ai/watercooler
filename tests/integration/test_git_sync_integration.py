import os
import shutil
import subprocess
from pathlib import Path
import pytest


pytestmark = pytest.mark.skipif(
    os.getenv("GIT_SYNC_IT") != "1",
    reason="Set GIT_SYNC_IT=1 to run integration tests",
)


def _run(cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True)


def test_concurrent_appends(tmp_path):
    origin = tmp_path / "origin.git"
    _run(["git", "init", "--bare", str(origin)])

    # Clone A and B
    a = tmp_path / "a"
    b = tmp_path / "b"
    _run(["git", "clone", str(origin), str(a)])
    _run(["git", "clone", str(origin), str(b)])

    # Create topic file in A and push
    (a / ".watercooler").mkdir(parents=True)
    topic = a / ".watercooler" / "it-topic.md"
    topic.write_text("# it-topic\n\n", encoding="utf-8")
    _run(["git", "add", "-A"], cwd=a)
    _run(["git", "commit", "-m", "init"], cwd=a)
    _run(["git", "push"], cwd=a)

    # Modify in both clones before pulling
    # A appends a line
    with (a / ".watercooler" / "it-topic.md").open("a", encoding="utf-8") as f:
        f.write("a1\n")
    _run(["git", "add", ".watercooler/it-topic.md"], cwd=a)
    _run(["git", "commit", "-m", "a1"], cwd=a)
    _run(["git", "push"], cwd=a)

    # Ensure B is up-to-date before editing (mirrors with_sync initial pull)
    _run(["git", "pull", "--rebase", "--autostash"], cwd=b)
    (b / ".watercooler").mkdir(exist_ok=True)
    # B appends a different line (non-conflicting)
    with (b / ".watercooler" / "it-topic.md").open("a", encoding="utf-8") as f:
        f.write("b1\n")
    _run(["git", "add", ".watercooler/it-topic.md"], cwd=b)
    _run(["git", "commit", "-m", "b1"], cwd=b)

    # Now push B
    _run(["git", "push"], cwd=b)

    # Final state: both entries present after pulling into A
    _run(["git", "pull", "--rebase", "--autostash"], cwd=a)
    content = (a / ".watercooler" / "it-topic.md").read_text(encoding="utf-8")
    assert "a1" in content and "b1" in content


@pytest.mark.xfail(reason="Same-hunk concurrent edits cause merge conflicts by design; manual resolution required")
def test_concurrent_same_hunk_conflict_xfail(tmp_path):
    origin = tmp_path / "origin.git"
    _run(["git", "init", "--bare", str(origin)])

    a = tmp_path / "a"
    b = tmp_path / "b"
    _run(["git", "clone", str(origin), str(a)])
    _run(["git", "clone", str(origin), str(b)])

    # Seed file
    (a / ".watercooler").mkdir(parents=True)
    p = a / ".watercooler" / "conflict.md"
    p.write_text("# conflict\n\n", encoding="utf-8")
    _run(["git", "add", "-A"], cwd=a)
    _run(["git", "commit", "-m", "init"], cwd=a)
    _run(["git", "push"], cwd=a)

    # Both clones modify the same region without pulling in between
    (a / ".watercooler" / "conflict.md").write_text("# conflict\n\na\n", encoding="utf-8")
    _run(["git", "add", ".watercooler/conflict.md"], cwd=a)
    _run(["git", "commit", "-m", "a"], cwd=a)

    (b / ".watercooler").mkdir(exist_ok=True)
    (b / ".watercooler" / "conflict.md").write_text("# conflict\n\nb\n", encoding="utf-8")
    _run(["git", "add", ".watercooler/conflict.md"], cwd=b)
    _run(["git", "commit", "-m", "b"], cwd=b)

    # Push A then B; B will fail to rebase automatically
    _run(["git", "push"], cwd=a)
    _run(["git", "pull", "--rebase", "--autostash"], cwd=b)  # expected to conflict
