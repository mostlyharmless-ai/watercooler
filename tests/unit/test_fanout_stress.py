"""Phase B step 3 — fan-out stress test (#904).

The regression guard for the data-loss bug. Fires N concurrent producers that each
append an entry to the graph and enqueue a commit task, while ONE CommitterDaemon
drains concurrently. Asserts the #904 invariants:

  * ZERO data loss — every enqueued entry ends up committed (no clobbering).
  * commit-count << entry-count — group-commit batching (vs one commit per entry).
  * no stale worktree lock left behind (the #903 self-block).
  * clean working tree / linearized history (nothing uncommitted).

Push is mocked: the race the single-writer daemon eliminates is the LOCAL worktree
commit race (#904), which is independent of the network push.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

from watercooler_mcp.daemons.committer import CommitterDaemon
from watercooler_mcp.memory_queue.queue import MemoryTaskQueue
from watercooler_mcp.memory_queue.task import MemoryTask

N = 24
N_TOPICS = 4


def _init_repo(threads_dir: Path):
    from git import Repo

    threads_dir.mkdir(parents=True, exist_ok=True)
    repo = Repo.init(threads_dir)
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "Test")
        cw.set_value("user", "email", "test@example.com")
    (threads_dir / "README.md").write_text("seed\n", encoding="utf-8")
    repo.git.add("-A")
    repo.index.commit("seed")
    return repo


def _append_entry(threads_dir: Path, topic: str, entry_id: str) -> None:
    g = threads_dir / "graph" / "baseline" / "threads" / topic
    g.mkdir(parents=True, exist_ok=True)
    with (g / "entries.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"entry_id": entry_id, "topic": topic, "body": entry_id}) + "\n")
    md = threads_dir / "threads"
    md.mkdir(parents=True, exist_ok=True)
    (md / f"{topic}.md").write_text(f"# {topic}\n{entry_id}\n", encoding="utf-8")


def _commit_count(threads_dir: Path) -> int:
    from git import Repo

    return len(list(Repo(threads_dir).iter_commits()))


def test_concurrent_fanout_no_data_loss_and_batched(tmp_path):
    from git import Repo

    threads_dir = tmp_path / "threads"
    _init_repo(threads_dir)
    q = MemoryTaskQueue(queue_dir=tmp_path / "cq")
    daemon = CommitterDaemon(commit_queue=q, interval=0.1, max_batch_size=50)

    topics = [f"topic-{i % N_TOPICS}" for i in range(N)]
    expected_ids = {f"E{i:03d}" for i in range(N)}
    # Serialize the graph append the way the real per-topic short lock does; the
    # bug under test is the COMMIT race, not the append.
    append_lock = threading.Lock()

    def producer(i: int) -> None:
        eid = f"E{i:03d}"
        with append_lock:
            _append_entry(threads_dir, topics[i], eid)
        q.enqueue(MemoryTask(
            backend="commit", entry_id=eid, topic=topics[i],
            threads_dir=str(threads_dir), content=f"say {eid}",
        ))

    before = _commit_count(threads_dir)
    with patch("watercooler_mcp.sync.primitives.push_with_retry", return_value=True):
        daemon.start()
        try:
            workers = [threading.Thread(target=producer, args=(i,)) for i in range(N)]
            for w in workers:
                w.start()
            for w in workers:
                w.join()
            # Drain.
            deadline = time.monotonic() + 30
            while q.pending_count() + q.running_count() > 0 and time.monotonic() < deadline:
                daemon.wake()
                time.sleep(0.05)
        finally:
            daemon.stop(timeout=5)

    after = _commit_count(threads_dir)
    repo = Repo(threads_dir)

    # 1. ZERO DATA LOSS — every entry id is present on disk and committed.
    seen: set[str] = set()
    for topic in set(topics):
        p = threads_dir / "graph" / "baseline" / "threads" / topic / "entries.jsonl"
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                seen.add(json.loads(line)["entry_id"])
    assert seen == expected_ids, f"data loss: missing {expected_ids - seen}"

    # 2. Everything committed (clean working tree — no clobbered/uncommitted state).
    assert not repo.is_dirty(), "uncommitted/regressed state after drain"
    assert q.pending_count() + q.running_count() == 0

    # 3. GROUP-COMMIT BATCHING — far fewer commits than entries.
    commits = after - before
    assert 1 <= commits < N, f"expected batching (commits<{N}), got {commits}"

    # 4. No stale worktree lock left behind (#903 self-block).
    assert not (threads_dir / ".watercooler" / "locks" / "_worktree.lock").exists()
