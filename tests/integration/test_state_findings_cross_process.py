"""Cross-process safety regression test for ``daemons.state.append_findings``.

Move 5 wraps ``append_findings``, ``acknowledge_finding`` and
``save_checkpoint`` with the ``file_lock`` primitive so concurrent
MCP server processes cannot interleave JSONL appends or race the
read-modify-rewrite cycle for compaction/acknowledgement.

This test spawns N processes that each call ``append_findings`` K
times against the same daemon namespace and asserts that the
findings.jsonl file ends up with exactly N×K parseable lines.

Without the migration, a baseline run (lockless, only the in-process
``threading.Lock``) can produce torn lines or interleaved appends.
After the migration, the cross-process advisory lock serialises
writes across the process boundary.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import pytest

# Avoid importing the watercooler_mcp top-level module in the worker
# (which spawns sub-services) by importing only the leaf state module.
# We inject WATERCOOLER_DAEMONS_ROOT env to keep the test sandboxed.


def _worker(
    daemon_root: str, daemon_name: str, namespace: str, worker_id: int, n_writes: int
) -> None:
    """Worker: append n_writes findings under the shared namespace.

    The daemon storage root is overridden by directly assigning the
    module attribute ``_state._DEFAULT_DAEMONS_DIR``. The env var
    is set for forward-compatibility only — if ``_daemons_dir()`` is
    ever refactored to read it, the test will continue to sandbox
    correctly. Today only the module-attribute assignment matters.
    """
    os.environ["WATERCOOLER_DAEMONS_ROOT"] = daemon_root
    # Module-attribute monkeypatch — the load-bearing override.
    from watercooler_mcp.daemons import state as _state

    _state._DEFAULT_DAEMONS_DIR = Path(daemon_root)
    from watercooler_mcp.daemons.state import Finding, append_findings, build_finding_id

    for i in range(n_writes):
        fid = build_finding_id(
            scope_id="test:repo",
            daemon_name=daemon_name,
            topic="t",
            category="c",
            entry_id=f"w{worker_id}-{i}",
        )
        finding = Finding(
            finding_id=fid,
            daemon_name=daemon_name,
            severity="info",
            category="c",
            topic="t",
            entry_id=f"w{worker_id}-{i}",
            message=f"worker {worker_id} write {i}" + " " * 200,
            details={"worker": worker_id, "i": i},
            created_at=time.time(),
        )
        append_findings(daemon_name, [finding], namespace=namespace)


@pytest.mark.skipif(sys.platform == "win32", reason="multiprocessing fork-safe path")
def test_append_findings_cross_process_no_interleave(tmp_path: Path) -> None:
    """N processes × K appends → exactly N×K parseable JSONL lines."""
    daemon_root = tmp_path / "daemons"
    daemon_root.mkdir()
    daemon_name = "test_daemon"
    namespace = "test_scope"
    n_workers = 6
    n_writes = 30

    # Force fork start method to avoid re-importing watercooler at top-level
    # in the child (which spawns LLM/embed/FalkorDB threads via
    # watercooler_mcp.startup). The child inherits the parent's
    # already-imported state module.
    ctx = mp.get_context("fork")
    procs = [
        ctx.Process(
            target=_worker,
            args=(str(daemon_root), daemon_name, namespace, w, n_writes),
        )
        for w in range(n_workers)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
        assert p.exitcode == 0, f"worker exited with {p.exitcode}"

    # Locate the findings file (state._daemon_dir sanitises namespace).
    safe_ns = namespace  # no unsafe chars in this fixture
    findings_path = daemon_root / safe_ns / daemon_name / "findings.jsonl"
    assert findings_path.exists()

    raw = findings_path.read_text()
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    assert (
        len(lines) == n_workers * n_writes
    ), f"expected {n_workers * n_writes} lines, got {len(lines)}"

    seen: set[tuple[int, int]] = set()
    for line in lines:
        data: dict[str, Any] = json.loads(line)
        details = data["details"]
        seen.add((details["worker"], details["i"]))
    assert len(seen) == n_workers * n_writes, "duplicate or missing finding entries"

    # Confirm the lock sentinel was created.
    lock_path = daemon_root / safe_ns / daemon_name / ".findings.lock"
    assert lock_path.exists(), "expected .findings.lock sentinel to exist"
    # Lock file should be 0o600.
    mode = lock_path.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0o600 perms on lock, got {oct(mode)}"

    shutil.rmtree(daemon_root)


def _hold_findings_lock(
    daemon_root: str,
    daemon_name: str,
    namespace: str,
    hold_s: float,
    ready_path: str,
) -> None:
    """Worker that acquires the same ``.findings.lock`` and holds it."""
    from watercooler_mcp.daemons import state as _state

    _state._DEFAULT_DAEMONS_DIR = Path(daemon_root)
    from watercooler_mcp.sync.file_lock import file_lock

    lock_path = _state._findings_lock_path(daemon_name, namespace=namespace)
    with file_lock(lock_path, timeout=10.0):
        Path(ready_path).touch()
        time.sleep(hold_s)


@pytest.mark.skipif(sys.platform == "win32", reason="multiprocessing fork-safe path")
def test_acknowledge_finding_returns_false_on_lock_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``acknowledge_finding`` must preserve its bool-only failure contract.

    Lock-acquisition timeout returns ``False`` with a WARNING log, never
    raises ``FileLockError`` — many callers do
    ``if not acknowledge_finding(...)`` and rely on that path catching
    every failure mode.
    """
    daemon_root = tmp_path / "daemons"
    daemon_root.mkdir()
    daemon_name = "ack_timeout_daemon"
    namespace = "ack_scope"

    from watercooler_mcp.daemons import state as state_mod

    monkeypatch.setattr(state_mod, "_DEFAULT_DAEMONS_DIR", daemon_root)
    # Shorten the timeout so the test runs in well under a second.
    monkeypatch.setattr(state_mod, "_FINDINGS_LOCK_TIMEOUT_S", 0.2)

    # Seed a single finding to ack so the function reaches the lock,
    # not the early "no findings file" return.
    fid = state_mod.build_finding_id(
        scope_id="test:repo",
        daemon_name=daemon_name,
        topic="t",
        category="c",
        entry_id="seed",
    )
    seed = state_mod.Finding(
        finding_id=fid,
        daemon_name=daemon_name,
        severity="info",
        category="c",
        topic="t",
        entry_id="seed",
        message="seed",
        details={},
        created_at=time.time(),
    )
    state_mod.append_findings(daemon_name, [seed], namespace=namespace)

    ready = tmp_path / "ready.flag"
    ctx = mp.get_context("fork")
    holder = ctx.Process(
        target=_hold_findings_lock,
        args=(str(daemon_root), daemon_name, namespace, 2.0, str(ready)),
    )
    holder.start()
    try:
        deadline = time.monotonic() + 5.0
        while not ready.exists():
            if time.monotonic() > deadline:
                pytest.fail("holder process never acquired the lock")
            time.sleep(0.05)

        result = state_mod.acknowledge_finding(daemon_name, fid, namespace=namespace)
        # Bool-only contract: timeout returns False, does not raise.
        assert result is False
    finally:
        holder.join(timeout=10)


def test_acknowledge_finding_returns_false_on_unsupported_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``FileLockUnsupportedError`` must also flow through the bool path.

    ``FileLockUnsupportedError`` and ``FileLockError`` are sibling
    direct subclasses of ``OSError``. The production code uses
    ``except OSError`` which catches both. This test simulates the
    unsupported-platform case by monkeypatching the lock helper to
    raise ``FileLockUnsupportedError`` on entry. The acknowledge
    path must still return ``False``, never propagate, to preserve
    the bool-only contract.
    """
    daemon_root = tmp_path / "daemons"
    daemon_root.mkdir()
    daemon_name = "ack_unsup_daemon"
    namespace = "ack_scope"

    from watercooler_mcp.daemons import state as state_mod
    from watercooler_mcp.sync.file_lock import FileLockUnsupportedError

    monkeypatch.setattr(state_mod, "_DEFAULT_DAEMONS_DIR", daemon_root)

    fid = state_mod.build_finding_id(
        scope_id="test:repo",
        daemon_name=daemon_name,
        topic="t",
        category="c",
        entry_id="seed",
    )
    seed = state_mod.Finding(
        finding_id=fid,
        daemon_name=daemon_name,
        severity="info",
        category="c",
        topic="t",
        entry_id="seed",
        message="seed",
        details={},
        created_at=time.time(),
    )
    state_mod.append_findings(daemon_name, [seed], namespace=namespace)

    from contextlib import contextmanager

    @contextmanager
    def _raise_unsupported(*_args, **_kwargs):
        raise FileLockUnsupportedError("simulated unsupported platform")
        yield  # pragma: no cover — generator must contain a yield to be a CM

    monkeypatch.setattr(state_mod, "file_lock", _raise_unsupported)

    result = state_mod.acknowledge_finding(daemon_name, fid, namespace=namespace)
    assert result is False, "FileLockUnsupportedError must return False, not propagate"


def test_per_pair_threading_lock_does_not_block_unrelated_daemons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-(daemon, namespace) thread locks keep unrelated paths independent.

    Previously a single module-global ``threading.Lock`` guarded all
    daemons. Combined with cross-process ``file_lock`` wrapping, a
    slow file-lock acquire on daemon A would freeze in-process writes
    to daemon B for the full timeout. The fix uses one
    ``threading.Lock`` per ``(daemon_name, namespace)`` pair.

    Asserts the cache produces distinct lock objects for distinct
    keys (the structural property that prevents cross-daemon
    blocking) and reuses the same lock for the same key (the
    correctness property that prevents intra-daemon races).
    """
    from watercooler_mcp.daemons import state as state_mod

    # Reset the cache so the test is isolated.
    monkeypatch.setattr(state_mod, "_findings_locks", {})

    lock_a_ns1 = state_mod._get_findings_lock("daemon_a", "ns1")
    lock_b_ns1 = state_mod._get_findings_lock("daemon_b", "ns1")
    lock_a_ns2 = state_mod._get_findings_lock("daemon_a", "ns2")
    lock_a_ns1_again = state_mod._get_findings_lock("daemon_a", "ns1")

    assert lock_a_ns1 is not lock_b_ns1, "different daemons must use different locks"
    assert lock_a_ns1 is not lock_a_ns2, "different namespaces must use different locks"
    assert lock_b_ns1 is not lock_a_ns2
    assert lock_a_ns1 is lock_a_ns1_again, "same key must reuse the same lock"


def test_lock_key_uses_sanitised_namespace_matching_filesystem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Threading-lock key must match the sanitised on-disk path.

    ``_daemon_dir`` sanitises namespace via ``re.sub`` so any chars
    outside ``[A-Za-z0-9_:-]`` collapse to ``_``. Two raw namespaces
    that resolve to the same on-disk lock file (e.g., ``"foo|bar"``
    and ``"foo_bar"`` — the ``|`` is sanitised to ``_``) MUST share
    the same in-process threading lock; otherwise threads from those
    raw namespaces pile up on the file lock instead of serialising
    cheaply in-process. This regression test asserts the threading
    layer uses the sanitised key.
    """
    from watercooler_mcp.daemons import state as state_mod

    monkeypatch.setattr(state_mod, "_findings_locks", {})

    # ``foo|bar`` and ``foo_bar`` collapse to ``foo_bar`` after the
    # sanitiser, so both should map to the SAME threading lock.
    raw_with_pipe = state_mod._get_findings_lock("daemon_x", "foo|bar")
    raw_with_underscore = state_mod._get_findings_lock("daemon_x", "foo_bar")
    assert raw_with_pipe is raw_with_underscore, (
        "raw namespaces that collapse to the same sanitised form "
        "must share the same threading lock"
    )

    # Sanity: a truly distinct namespace still produces a distinct lock.
    other = state_mod._get_findings_lock("daemon_x", "completely-different")
    assert raw_with_pipe is not other


def test_acknowledge_finding_returns_false_on_unexpected_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bool-only contract absorbs any ``OSError`` from the lock layer.

    Beyond ``FileLockError`` and ``FileLockUnsupportedError``, the
    lock primitive can raise plain ``OSError`` (e.g., ``ENOMEM``,
    ``ENFILE`` from fd exhaustion, ``EACCES`` on a corrupted lock
    file). The except clause widens to ``OSError`` so all of these
    flow through the bool path rather than propagating.
    """
    daemon_root = tmp_path / "daemons"
    daemon_root.mkdir()
    daemon_name = "ack_oserror_daemon"
    namespace = "ack_scope"

    from watercooler_mcp.daemons import state as state_mod

    monkeypatch.setattr(state_mod, "_DEFAULT_DAEMONS_DIR", daemon_root)

    fid = state_mod.build_finding_id(
        scope_id="test:repo",
        daemon_name=daemon_name,
        topic="t",
        category="c",
        entry_id="seed",
    )
    seed = state_mod.Finding(
        finding_id=fid,
        daemon_name=daemon_name,
        severity="info",
        category="c",
        topic="t",
        entry_id="seed",
        message="seed",
        details={},
        created_at=time.time(),
    )
    state_mod.append_findings(daemon_name, [seed], namespace=namespace)

    from contextlib import contextmanager

    @contextmanager
    def _raise_enomem(*_args, **_kwargs):
        raise OSError(12, "Cannot allocate memory")  # ENOMEM
        yield  # pragma: no cover

    monkeypatch.setattr(state_mod, "file_lock", _raise_enomem)

    result = state_mod.acknowledge_finding(daemon_name, fid, namespace=namespace)
    assert result is False, "Unexpected OSError must return False, not propagate"
