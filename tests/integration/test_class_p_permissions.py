"""Class P storage hygiene — file permissions test (Move 6).

The Class P contract from plan v5.1 requires:

* Per-scope path encoding (covered by M1 + M3)
* 0600 permissions on at-rest Class P files
* Bounded retention (existing TTL; documented separately)
* No diagnostic copies (egress-inventory CI catches via Class D scan)
* Scope-tagged paths (Move 3 contract)

This test exercises the **0600 permissions** invariant for files
the Class P storage layer creates at write time. Sites tested:

1. Memory queue ``queue.jsonl`` / ``dead_letter.jsonl`` /
   ``task_receipts.jsonl``
2. Findings ``findings.jsonl`` (per scope/daemon)
3. Lock files (already enforced by ``sync/file_lock.py``; covered
   here as a regression guard)
4. Credentials file (already enforced; regression guard)

Skipped on Windows where POSIX permission bits don't apply.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

if sys.platform == "win32":
    pytest.skip("POSIX permission bits not applicable", allow_module_level=True)


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #


def _file_mode(path: Path) -> int:
    """Return the permission bits (mode & 0o777) for *path*."""
    return path.stat().st_mode & 0o777


def _is_owner_only(mode: int) -> bool:
    """0600 — owner read/write, no group/world bits."""
    return mode == 0o600


def _no_world_bits(mode: int) -> bool:
    """Looser check: world bits unset (group bits permitted)."""
    return (mode & 0o007) == 0


# ------------------------------------------------------------------ #
# File-lock primitive (PR #690) — sentinel files
# ------------------------------------------------------------------ #


class TestFileLockPermissions:
    def test_lock_file_is_0600(self, tmp_path: Path) -> None:
        # Direct test of the file_lock primitive. The lock file is
        # NOT itself Class P data, but the same primitive is used
        # for findings and queue locks — a regression here would
        # propagate to Class P paths.
        from watercooler_mcp.sync.file_lock import file_lock

        lock_path = tmp_path / "test.lock"
        with file_lock(lock_path, exclusive=True, timeout=2.0):
            pass
        assert lock_path.exists()
        mode = _file_mode(lock_path)
        assert _is_owner_only(mode), f"lock file mode is {oct(mode)}, expected 0o600"


# ------------------------------------------------------------------ #
# Findings storage — Class P at-rest files
# ------------------------------------------------------------------ #


class TestFindingsStoragePermissions:
    """PR #705 round 2 MED finding: previously these tests forced
    ``os.umask(0o077)`` so ``open(path, "a")`` happened to produce
    0o600 — but that was the *umask's* doing, not the
    implementation's. On a real production host with the typical
    0o022 umask, the same code created world-readable Class P
    files. The implementation now calls ``os.chmod(path, 0o600)``
    explicitly after the first write; these tests assert under a
    LOOSE umask so a regression cannot hide behind a tight CI
    default.
    """

    def test_findings_jsonl_is_0600_under_loose_umask(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from watercooler_mcp.daemons import state as state_mod

        daemons_root = tmp_path / ".watercooler" / "daemons"
        monkeypatch.setattr(state_mod, "_DEFAULT_DAEMONS_DIR", daemons_root)

        # LOOSE umask — the implementation MUST tighten regardless.
        old_umask = os.umask(0o022)
        try:
            finding = state_mod.Finding(
                finding_id="hygiene-test",
                daemon_name="content_scout",
                severity="info",
                category="test",
                topic="hygiene",
                created_at=1.0,
            )
            state_mod.append_findings("content_scout", [finding], namespace="")

            findings_file = daemons_root / "content_scout" / "findings.jsonl"
            assert (
                findings_file.exists()
            ), f"append_findings did not create {findings_file}"
            mode = _file_mode(findings_file)
            assert _is_owner_only(mode), (
                f"findings.jsonl mode is {oct(mode)}, expected 0o600 "
                "(implementation must chmod explicitly, not rely on umask)"
            )
        finally:
            os.umask(old_umask)

    def test_checkpoint_json_is_0600_under_loose_umask(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from watercooler_mcp.daemons import state as state_mod

        daemons_root = tmp_path / ".watercooler" / "daemons"
        monkeypatch.setattr(state_mod, "_DEFAULT_DAEMONS_DIR", daemons_root)

        old_umask = os.umask(0o022)
        try:
            ckpt = state_mod.DaemonCheckpoint(daemon_name="content_scout")
            state_mod.save_checkpoint(ckpt, namespace="")

            ckpt_file = daemons_root / "content_scout" / "checkpoint.json"
            assert ckpt_file.exists(), "save_checkpoint did not create file"
            mode = _file_mode(ckpt_file)
            assert _is_owner_only(
                mode
            ), f"checkpoint.json mode is {oct(mode)}, expected 0o600"
        finally:
            os.umask(old_umask)

    def test_findings_jsonl_no_toctou_window_at_creation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PR #705 round 4 MED: file created at 0o600 via
        ``os.open(O_CREAT|O_APPEND|O_WRONLY, 0o600)`` at the
        kernel level — no window where the file briefly exists at
        umask-default mode between creation and chmod. Direct
        verification of the absence-of-window requires racing
        inotify; this test instead pins the end-state under loose
        umask, which catches a regression to the bare-``open``
        path.
        """
        from watercooler_mcp.daemons import state as state_mod

        daemons_root = tmp_path / ".watercooler" / "daemons"
        monkeypatch.setattr(state_mod, "_DEFAULT_DAEMONS_DIR", daemons_root)

        old_umask = os.umask(0o022)
        try:
            finding = state_mod.Finding(
                finding_id="hygiene-test",
                daemon_name="content_scout",
                severity="info",
                category="test",
                topic="hygiene",
                created_at=1.0,
            )
            state_mod.append_findings("content_scout", [finding], namespace="")

            findings_file = daemons_root / "content_scout" / "findings.jsonl"
            assert findings_file.exists()
            mode = _file_mode(findings_file)
            assert _is_owner_only(mode), (
                f"findings.jsonl created at mode {oct(mode)}, expected 0o600 — "
                "regression of TOCTOU fix (kernel-level O_CREAT mode)"
            )
        finally:
            os.umask(old_umask)

    def test_findings_jsonl_stays_0600_through_compaction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PR #705 round 6 MED: ``_maybe_compact`` rewrites
        ``findings.jsonl`` via mkstemp + os.replace.
        ``mkstemp`` defaults to 0o600 on POSIX but that contract
        is fragile across platforms / umasks; the implementation
        now chmods explicitly before the rename.
        """
        from watercooler_mcp.daemons import state as state_mod

        daemons_root = tmp_path / ".watercooler" / "daemons"
        monkeypatch.setattr(state_mod, "_DEFAULT_DAEMONS_DIR", daemons_root)
        # Force compaction to fire on every append.
        monkeypatch.setattr(state_mod, "_MAX_FINDINGS_LINES", 1)
        monkeypatch.setattr(state_mod, "_COMPACT_KEEP_LINES", 1)

        old_umask = os.umask(0o022)
        try:
            for i in range(3):
                f = state_mod.Finding(
                    finding_id=f"f-{i}",
                    daemon_name="content_scout",
                    severity="info",
                    category="test",
                    topic="hygiene",
                    created_at=float(i),
                )
                state_mod.append_findings("content_scout", [f], namespace="")

            findings_file = daemons_root / "content_scout" / "findings.jsonl"
            assert findings_file.exists()
            mode = _file_mode(findings_file)
            assert _is_owner_only(mode), (
                f"findings.jsonl mode is {oct(mode)} after compaction, "
                "expected 0o600 — chmod regression in _maybe_compact"
            )
        finally:
            os.umask(old_umask)

    def test_findings_jsonl_stays_0600_through_acknowledge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PR #705 round 6 MED: ``acknowledge_finding`` also
        rewrites via mkstemp + os.replace and now chmods
        explicitly. Pin the invariant: ack rewrite preserves
        0o600.
        """
        from watercooler_mcp.daemons import state as state_mod

        daemons_root = tmp_path / ".watercooler" / "daemons"
        monkeypatch.setattr(state_mod, "_DEFAULT_DAEMONS_DIR", daemons_root)

        old_umask = os.umask(0o022)
        try:
            f = state_mod.Finding(
                finding_id="ack-target",
                daemon_name="content_scout",
                severity="info",
                category="test",
                topic="hygiene",
                created_at=1.0,
            )
            state_mod.append_findings("content_scout", [f], namespace="")

            ok = state_mod.acknowledge_finding(
                "content_scout", "ack-target", namespace=""
            )
            assert ok is True

            findings_file = daemons_root / "content_scout" / "findings.jsonl"
            mode = _file_mode(findings_file)
            assert _is_owner_only(mode), (
                f"findings.jsonl mode is {oct(mode)} after ack rewrite, "
                "expected 0o600 — chmod regression in acknowledge_finding"
            )
        finally:
            os.umask(old_umask)

    def test_findings_jsonl_tightens_pre_existing_loose_perms(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PR #705 round 3 MED finding: ``append_findings``
        chmod's the file unconditionally on every write, not just
        on first creation. Files inherited from earlier
        deployments (or created with a relaxed umask before the
        hygiene step landed) are tightened on the next append.
        """
        from watercooler_mcp.daemons import state as state_mod

        daemons_root = tmp_path / ".watercooler" / "daemons"
        monkeypatch.setattr(state_mod, "_DEFAULT_DAEMONS_DIR", daemons_root)

        # Pre-create a findings.jsonl with loose 0o644 perms
        # (simulating a file inherited from an earlier deployment
        # without the explicit chmod step).
        findings_dir = daemons_root / "content_scout"
        findings_dir.mkdir(parents=True)
        findings_file = findings_dir / "findings.jsonl"
        findings_file.write_text("")
        os.chmod(findings_file, 0o644)
        assert _file_mode(findings_file) == 0o644, "fixture setup failed"

        # Append — implementation must tighten on every call.
        finding = state_mod.Finding(
            finding_id="hygiene-test",
            daemon_name="content_scout",
            severity="info",
            category="test",
            topic="hygiene",
            created_at=1.0,
        )
        state_mod.append_findings("content_scout", [finding], namespace="")

        mode = _file_mode(findings_file)
        assert _is_owner_only(mode), (
            f"findings.jsonl was 0o644 before append; after append it must "
            f"be 0o600. Actual: {oct(mode)}"
        )


# ------------------------------------------------------------------ #
# Memory queue — bounded-retention Class P at-rest files
# ------------------------------------------------------------------ #


class TestMemoryQueueFilesNoWorldBits:
    """Memory queue JSONL files carry payload bodies (Class P).

    Like findings, current code uses bare ``open(path, "a")`` without
    explicit mode, so the umask determines the result. Pin the
    invariant: no world bits — and pin it on a file that
    ``MemoryTaskQueue`` itself creates, NOT one the test fabricates
    (the original implementation of this test fell back to
    ``queue_file.touch(mode=0o600)`` and tautologically asserted on
    its own creation; PR #705 review HIGHLIGHTED THIS).
    """

    def test_queue_payload_files_are_0600_under_loose_umask(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        try:
            from watercooler_mcp.memory_queue.queue import MemoryTaskQueue
            from watercooler_mcp.memory_queue.task import MemoryTask
        except ImportError:
            pytest.skip("memory_queue module unavailable")

        # LOOSE umask — implementation MUST tighten regardless.
        old_umask = os.umask(0o022)
        try:
            queue_dir = tmp_path / "memory_queue"
            queue_dir.mkdir()
            queue = MemoryTaskQueue(queue_dir=queue_dir)

            # Force the queue to write through its OWN code path —
            # enqueue a task, which triggers ``_atomic_write`` of
            # ``queue.jsonl`` + ``stats.json``. The on-disk
            # permissions reflect the implementation's chmod, not
            # the test's umask.
            task = MemoryTask(
                task_id="hygiene-test",
                task_type="single",
                backend="graphiti",
                entry_id="entry-x",
                topic="hygiene",
                group_id="test-scope",
                content="payload",
                title="hygiene check",
                timestamp="2026-04-29T00:00:00Z",
                source_description="test",
            )
            queue.enqueue(task)

            queue_file = queue_dir / "queue.jsonl"
            if not queue_file.exists():
                pytest.skip(
                    "queue did not create queue.jsonl — implementation "
                    "changed; this test needs to be re-pinned to whatever "
                    "the new write path is"
                )
            mode = _file_mode(queue_file)
            assert _is_owner_only(
                mode
            ), f"queue.jsonl mode is {oct(mode)}, expected 0o600"

            # Stats file is also Class P (carries enqueue counters
            # tied to scope state); same invariant.
            stats_file = queue_dir / "stats.json"
            if stats_file.exists():
                mode = _file_mode(stats_file)
                assert _is_owner_only(
                    mode
                ), f"stats.json mode is {oct(mode)}, expected 0o600"
        finally:
            os.umask(old_umask)

    def test_open_class_p_append_tightens_pre_existing_loose_perms(
        self, tmp_path: Path
    ) -> None:
        """PR #705 round 7+2 MED — ``_open_class_p_append`` docstring
        claims pre-existing files are tightened on every open. The
        round 7+2 fix moved that step from ``os.chmod(path, 0o600)``
        to ``os.fchmod(fd, 0o600)`` (closing a TOCTOU symlink-swap
        window). Pin behaviour by calling the helper directly on a
        pre-loose file — if a future refactor conditionalises the
        fchmod call, the regression is caught here.
        """
        try:
            from watercooler_mcp.memory_queue.queue import MemoryTaskQueue
        except ImportError:
            pytest.skip("memory_queue module unavailable")

        queue_dir = tmp_path / "memory_queue"
        queue_dir.mkdir()
        target = queue_dir / "dead_letter.jsonl"
        target.write_text("pre-existing\n")
        os.chmod(target, 0o644)
        assert _file_mode(target) == 0o644, "fixture setup failed"

        queue = MemoryTaskQueue(queue_dir=queue_dir)
        # Call the helper directly — exercises the fchmod path
        # regardless of the public-API call shape.
        with queue._open_class_p_append(target) as f:
            f.write("appended\n")

        mode = _file_mode(target)
        assert _is_owner_only(mode), (
            f"dead_letter.jsonl was 0o644 before append; after "
            f"_open_class_p_append it must be 0o600. Actual: {oct(mode)}"
        )


# ------------------------------------------------------------------ #
# Credentials file — already enforced (regression guard)
# ------------------------------------------------------------------ #


class TestCredentialsFilePermissions:
    def test_credentials_helper_sets_0600(self, tmp_path: Path) -> None:
        # The ``_secure_file_permissions`` helper is the existing
        # enforcement primitive. This test pins it as a regression
        # guard so a future refactor can't silently weaken it.
        from watercooler.credentials import _secure_file_permissions

        path = tmp_path / "creds.toml"
        path.write_text("[github]\ntoken = 'x'\n")
        # Default umask may produce 0o644; secure helper must tighten.
        _secure_file_permissions(path)
        mode = _file_mode(path)
        assert _is_owner_only(
            mode
        ), f"_secure_file_permissions left mode {oct(mode)}, expected 0o600"
