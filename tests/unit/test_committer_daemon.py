"""Phase B (#904) — CommitterDaemon: single-writer group-commit, isolated.

Verifies the core invariant: N enqueued commit tasks for a worktree are flushed
as ONE batched commit (cadence decoupled from write rate), all tasks are
completed via receipts, and push failure retries (never silently drops). The
daemon is tested in isolation — the live write path is not yet wired to it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from watercooler_mcp.daemons.committer import CommitterDaemon
from watercooler_mcp.memory_queue.queue import MemoryTaskQueue
from watercooler_mcp.memory_queue.task import MemoryTask


def _init_repo(threads_dir: Path):
    from git import Repo

    threads_dir.mkdir(parents=True, exist_ok=True)
    repo = Repo.init(threads_dir)
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "Test")
        cw.set_value("user", "email", "test@example.com")
    (threads_dir / "README.md").write_text("seed\n", encoding="utf-8")
    # Mirror production: the worktree lock lives under .watercooler/, which is
    # gitignored so a `git add -A` (committer fallback / reconcile sweep) never
    # stages the lock file as content.
    (threads_dir / ".gitignore").write_text(".watercooler/\n", encoding="utf-8")
    repo.git.add("-A")
    repo.index.commit("seed")
    return repo


def _write_topic_files(threads_dir: Path, topic: str, entry_id: str) -> None:
    """Create the standard graph + markdown projection paths for a topic so
    paths_to_stage_for_topic has real files to stage."""
    graph = threads_dir / "graph" / "baseline" / "threads" / topic
    graph.mkdir(parents=True, exist_ok=True)
    with (graph / "entries.jsonl").open("a", encoding="utf-8") as f:
        f.write(f'{{"entry_id":"{entry_id}","topic":"{topic}","body":"b"}}\n')
    md = threads_dir / "threads"
    md.mkdir(parents=True, exist_ok=True)
    (md / f"{topic}.md").write_text(f"# {topic}\n{entry_id}\n", encoding="utf-8")


def _commit_count(threads_dir: Path) -> int:
    from git import Repo

    return len(list(Repo(threads_dir).iter_commits()))


def test_batch_of_tasks_makes_one_commit_and_completes_all(tmp_path):
    threads_dir = tmp_path / "threads"
    _init_repo(threads_dir)
    _write_topic_files(threads_dir, "topicA", "E1")
    _write_topic_files(threads_dir, "topicA", "E2")

    q = MemoryTaskQueue(queue_dir=tmp_path / "cq")
    for eid in ("E1", "E2"):
        q.enqueue(MemoryTask(
            backend="commit", topic="topicA", entry_id=eid,
            threads_dir=str(threads_dir), content="say: E",
        ))
    assert q.pending_count() == 2

    before = _commit_count(threads_dir)
    daemon = CommitterDaemon(commit_queue=q, interval=5.0, max_batch_size=50)

    with patch("watercooler_mcp.sync.primitives.push_with_retry", return_value=True) as mock_push:
        findings = daemon.tick()

    # Single-writer group commit: TWO tasks -> exactly ONE commit + ONE push.
    assert _commit_count(threads_dir) == before + 1
    assert mock_push.call_count == 1
    # Both tasks completed (drained, no errors).
    assert q.pending_count() == 0
    assert q.running_count() == 0
    assert not [f for f in findings if f.severity in ("error", "warning")]


def test_push_failure_retries_not_drops(tmp_path):
    threads_dir = tmp_path / "threads"
    _init_repo(threads_dir)
    _write_topic_files(threads_dir, "topicB", "E9")

    q = MemoryTaskQueue(queue_dir=tmp_path / "cq2")
    q.enqueue(MemoryTask(
        backend="commit", topic="topicB", entry_id="E9",
        threads_dir=str(threads_dir), content="say: E9",
    ))
    daemon = CommitterDaemon(commit_queue=q, interval=5.0)

    before = _commit_count(threads_dir)
    with patch("watercooler_mcp.sync.primitives.push_with_retry", return_value=False):
        findings = daemon.tick()

    # Committed locally (data safe) ...
    assert _commit_count(threads_dir) == before + 1
    # ... but the task is retried (back to pending after backoff), not dropped,
    # and a push_failed warning is surfaced.
    assert q.depth() == 1  # still tracked (PENDING-with-backoff or retry)
    assert any(f.category == "push_failed" for f in findings)


def test_failed_push_then_retry_actually_repushes(tmp_path):
    """Regression guard (#906 review item 1): after a push failure the commit is
    local-only (index clean). The retry MUST re-attempt the push instead of
    short-circuiting on the clean index to a 'completed' receipt — which would
    strand the commit off origin AND falsely confirm a push that never happened.
    """
    threads_dir = tmp_path / "threads"
    _init_repo(threads_dir)
    _write_topic_files(threads_dir, "topicR", "E7")

    q = MemoryTaskQueue(queue_dir=tmp_path / "cqr")
    q.enqueue(MemoryTask(
        backend="commit", topic="topicR", entry_id="E7",
        threads_dir=str(threads_dir), content="say: E7",
    ))
    daemon = CommitterDaemon(commit_queue=q, interval=5.0)
    task = q.dequeue()  # RUNNING
    group = [task]
    before = _commit_count(threads_dir)

    # Batch 1 — push FAILS: content is committed locally, push unconfirmed.
    with patch("watercooler_mcp.sync.primitives.push_with_retry", return_value=False) as p1:
        f1 = daemon._commit_batch(str(threads_dir), group)
    assert _commit_count(threads_dir) == before + 1          # committed locally
    assert p1.call_count == 1
    assert f1 is not None and f1.category == "push_failed"
    # Receipt must NOT be completed — the push never landed.
    assert (q.get_receipt(task.task_id) or {}).get("terminal_state") != "completed"

    # Next tick re-picks the task (bypass the 30s retry backoff deterministically).
    task.mark_running()

    # Batch 2 — index is CLEAN (already committed) but the commit is UNPUSHED, so
    # the daemon must push again. This is the bug: the old code saw a clean index
    # and completed without pushing.
    with patch("watercooler_mcp.sync.primitives.push_with_retry", return_value=True) as p2:
        daemon._commit_batch(str(threads_dir), group)
    assert _commit_count(threads_dir) == before + 1          # NO second commit
    assert p2.call_count == 1                                # re-pushed (the fix)
    assert (q.get_receipt(task.task_id) or {}).get("terminal_state") == "completed"
    assert q.depth() == 0


def test_queue_survives_process_restart(tmp_path):
    """Write-behind durability: an enqueued commit task persists to queue_dir and
    is re-drained after the in-memory queue object is gone (process restart)."""
    threads_dir = tmp_path / "threads"
    _init_repo(threads_dir)
    _write_topic_files(threads_dir, "topicP", "E5")

    qdir = tmp_path / "cqp"
    q1 = MemoryTaskQueue(queue_dir=qdir)
    q1.enqueue(MemoryTask(
        backend="commit", topic="topicP", entry_id="E5",
        threads_dir=str(threads_dir), content="say: E5",
    ))
    del q1  # simulate process death — only queue_dir survives

    q2 = MemoryTaskQueue(queue_dir=qdir)  # fresh instance reloads from disk
    assert q2.pending_count() == 1        # the accepted write survived

    daemon = CommitterDaemon(commit_queue=q2, interval=5.0)
    before = _commit_count(threads_dir)
    with patch("watercooler_mcp.sync.primitives.push_with_retry", return_value=True):
        daemon.tick()
    assert _commit_count(threads_dir) == before + 1  # drained after restart
    assert q2.pending_count() == 0


def test_empty_queue_is_a_noop(tmp_path):
    q = MemoryTaskQueue(queue_dir=tmp_path / "cq3")
    # reconcile disabled so an empty tick can't touch any seed worktree.
    daemon = CommitterDaemon(commit_queue=q, interval=5.0, reconcile_interval=0.0)
    assert daemon.tick() == []


# --------------------------------------------------------------------------- #
# #907 follow-ups: reconciliation sweep + topic-less staging guard
# --------------------------------------------------------------------------- #

def _init_repo_with_remote(threads_dir: Path, tmp_path: Path):
    from git import Repo

    remote = tmp_path / "remote.git"
    Repo.init(remote, bare=True)
    repo = _init_repo(threads_dir)
    repo.create_remote("origin", str(remote))
    repo.git.push("--set-upstream", "origin", repo.active_branch.name)
    return repo


def test_reconcile_commits_taskless_dirty_worktree(tmp_path):
    """#907 pre-enqueue crash window: an entry appended to the graph with NO
    commit task (process died before enqueue) is flushed by the sweep."""
    threads_dir = tmp_path / "threads"
    _init_repo(threads_dir)
    _write_topic_files(threads_dir, "stranded", "E_LOST")  # dirty, no task, no remote

    q = MemoryTaskQueue(queue_dir=tmp_path / "cqs")
    daemon = CommitterDaemon(
        commit_queue=q, interval=5.0, reconcile_interval=60.0,
        threads_dir_override=str(threads_dir),
    )
    daemon._last_reconcile = 0.0  # force the sweep this tick
    before = _commit_count(threads_dir)

    findings = daemon.tick()

    from git import Repo
    assert _commit_count(threads_dir) == before + 1     # stranded entry committed
    assert not Repo(threads_dir).is_dirty()
    recon = [f for f in findings if f.category == "reconciled"]
    assert recon and "committed" in recon[0].details["actions"]


def test_reconcile_pushes_taskless_unpushed_commit(tmp_path):
    """#907 dropped-task / crash-after-commit: a local commit ahead of origin with
    no pending task is pushed by the sweep."""
    from git import Repo

    threads_dir = tmp_path / "threads"
    repo = _init_repo_with_remote(threads_dir, tmp_path)
    _write_topic_files(threads_dir, "drp", "E_DROP")
    repo.git.add("-A")
    repo.index.commit("local-only entry (no task)")
    assert daemon_unpushed(repo)  # precondition: ahead of origin

    q = MemoryTaskQueue(queue_dir=tmp_path / "cqd")
    daemon = CommitterDaemon(
        commit_queue=q, interval=5.0, reconcile_interval=60.0,
        threads_dir_override=str(threads_dir),
    )
    daemon._last_reconcile = 0.0

    findings = daemon.tick()

    recon = [f for f in findings if f.category == "reconciled"]
    assert recon and "pushed" in recon[0].details["actions"]
    assert not daemon_unpushed(repo)  # now on origin


def daemon_unpushed(repo) -> bool:
    branch = repo.active_branch
    tracking = branch.tracking_branch()
    if tracking is None:
        return True
    return any(True for _ in repo.iter_commits(f"{tracking.name}..{branch.name}"))


def test_reconcile_respects_interval(tmp_path):
    """The sweep runs at most once per reconcile_interval. The first tick (clock
    started at construction) is within the interval and must NOT reconcile."""
    threads_dir = tmp_path / "threads"
    _init_repo(threads_dir)
    _write_topic_files(threads_dir, "s", "E1")

    q = MemoryTaskQueue(queue_dir=tmp_path / "cqi")
    daemon = CommitterDaemon(
        commit_queue=q, interval=5.0, reconcile_interval=60.0,
        threads_dir_override=str(threads_dir),
    )
    before = _commit_count(threads_dir)

    findings1 = daemon.tick()  # within interval -> no reconcile
    assert _commit_count(threads_dir) == before
    assert not any(f.category == "reconciled" for f in findings1)

    daemon._last_reconcile = 0.0  # interval elapsed
    findings2 = daemon.tick()
    assert _commit_count(threads_dir) == before + 1
    assert any(f.category == "reconciled" for f in findings2)


def test_reconcile_noop_on_clean_worktree(tmp_path):
    threads_dir = tmp_path / "threads"
    _init_repo(threads_dir)  # clean, no remote

    q = MemoryTaskQueue(queue_dir=tmp_path / "cqn")
    daemon = CommitterDaemon(
        commit_queue=q, reconcile_interval=60.0,
        threads_dir_override=str(threads_dir),
    )
    daemon._last_reconcile = 0.0
    before = _commit_count(threads_dir)

    findings = daemon.tick()

    assert _commit_count(threads_dir) == before
    assert not any(
        f.category in ("reconciled", "reconcile_error") for f in findings
    )


def test_mixed_batch_with_topicless_task_stages_all(tmp_path):
    """#907 topic-less staging guard: a batch mixing a topic'd task with a
    topic-less one stages everything (`-A`), so no content is silently dropped."""
    from git import Repo

    threads_dir = tmp_path / "threads"
    _init_repo(threads_dir)
    _write_topic_files(threads_dir, "realtopic", "E_T")
    # Extra content a topic-less task would carry — NOT under any topic path.
    misc = threads_dir / "graph" / "baseline" / "misc.jsonl"
    misc.parent.mkdir(parents=True, exist_ok=True)
    misc.write_text('{"x":1}\n', encoding="utf-8")

    q = MemoryTaskQueue(queue_dir=tmp_path / "cqm")
    q.enqueue(MemoryTask(
        backend="commit", topic="realtopic", entry_id="E_T",
        threads_dir=str(threads_dir), content="c",
    ))
    q.enqueue(MemoryTask(
        backend="commit", topic="", entry_id="E_NONE",
        threads_dir=str(threads_dir), content="c",
    ))
    daemon = CommitterDaemon(
        commit_queue=q, interval=5.0, reconcile_interval=0.0,  # isolate batch logic
    )
    before = _commit_count(threads_dir)

    with patch("watercooler_mcp.sync.primitives.push_with_retry", return_value=True):
        daemon.tick()

    # One commit that captured BOTH the topic files AND the topic-less misc.jsonl
    # (old behavior staged only topic paths -> misc.jsonl left dirty).
    assert _commit_count(threads_dir) == before + 1
    assert not Repo(threads_dir).is_dirty()
