"""Per-repo worktree-creation serialization.

One MCP server serves many repos concurrently (daemons + tool calls in
threads). Two concurrent first-writes to the SAME repo must not race
``git worktree add`` into a failure that degrades to ``<repo>/_local`` — the
spurious local-only fallback operators hit in concurrent multi-repo use
(bug-sync-worktree-poisoning). ``_ensure_worktree`` serializes creation per
repo; different repos take different locks.
"""

from __future__ import annotations

import threading
import time

from watercooler_mcp import config


def test_worktree_create_lock_is_per_repo(tmp_path):
    a = tmp_path / "repoA"
    b = tmp_path / "repoB"
    a.mkdir()
    b.mkdir()

    la1 = config._worktree_create_lock(a)
    la2 = config._worktree_create_lock(a)
    lb = config._worktree_create_lock(b)

    assert la1 is la2  # same repo → same lock (cached)
    assert la1 is not lb  # different repo → different lock


def test_ensure_worktree_serializes_same_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKTREE_BASE", tmp_path / "wtbase")
    root = tmp_path / "repo"
    root.mkdir()

    state = {"current": 0, "max": 0}
    guard = threading.Lock()

    def _fake_locked(code_root, wt_path, push=True):
        with guard:
            state["current"] += 1
            state["max"] = max(state["max"], state["current"])
        time.sleep(0.02)  # widen the window a real `git worktree add` would race in
        with guard:
            state["current"] -= 1
        return wt_path

    monkeypatch.setattr(config, "_ensure_worktree_locked", _fake_locked)

    threads = [
        threading.Thread(target=lambda: config._ensure_worktree(root))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # The locked creation impl never ran concurrently for the same repo.
    assert state["max"] == 1


def test_ensure_worktree_allows_different_repos_concurrently(tmp_path, monkeypatch):
    """Different repos take different locks — creation is NOT globally serialized."""
    monkeypatch.setattr(config, "WORKTREE_BASE", tmp_path / "wtbase")
    roots = [tmp_path / f"repo{i}" for i in range(4)]
    for r in roots:
        r.mkdir()

    state = {"current": 0, "max": 0}
    guard = threading.Lock()
    two_inside = threading.Event()

    def _fake_locked(code_root, wt_path, push=True):
        with guard:
            state["current"] += 1
            state["max"] = max(state["max"], state["current"])
            if state["current"] >= 2:
                two_inside.set()
        two_inside.wait(timeout=2.0)  # hold until ≥2 are concurrently inside
        with guard:
            state["current"] -= 1
        return wt_path

    monkeypatch.setattr(config, "_ensure_worktree_locked", _fake_locked)

    threads = [
        threading.Thread(target=lambda r=r: config._ensure_worktree(r)) for r in roots
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert state["max"] >= 2  # different repos ran creation concurrently


def test_create_filelock_serializes_across_processes(tmp_path, monkeypatch):
    """The cross-process file lock genuinely excludes a second holder — so a CLI
    write in another process can't race the server's first-creation."""
    from watercooler.lock import AdvisoryLock

    monkeypatch.setattr(config, "WORKTREE_BASE", tmp_path / "wtbase")
    wt_path = config._worktree_path_for(tmp_path / "repo")

    held = config._acquire_create_filelock(wt_path)
    assert held is not None

    # A second acquirer (simulating another process) cannot take it while held.
    lock_path = wt_path.parent / f"{wt_path.name}.create.lock"
    contender = AdvisoryLock(lock_path, ttl=120, timeout=0)
    assert contender.acquire() is False

    held.release()
    # Released — now it's available.
    assert contender.acquire() is True
    contender.release()
