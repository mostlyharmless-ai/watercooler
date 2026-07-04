"""Unit coverage for the container artifact chown (#920).

Benchmark containers run as root (the T3 path bind-mounts under /root/.watercooler),
so files written to the rw /output bind mount land root-owned on the host. The
harness chowns them back to the invoking user from inside the container before
teardown. These tests pin that behavior with a mocked container (no Docker).
"""

from __future__ import annotations

import os
import threading
from unittest.mock import MagicMock

from tests.benchmarks.wcbench.tracks.agent_value import _chown_host_artifacts


def test_chown_runs_as_root_with_host_uid_gid(monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(os, "getgid", lambda: 2000, raising=False)
    c = MagicMock()
    c.exec_run.return_value = (0, b"")

    _chown_host_artifacts(c, ["/output"])

    c.exec_run.assert_called_once()
    args, kwargs = c.exec_run.call_args
    assert args[0] == ["chown", "-R", "1000:2000", "/output"]
    assert kwargs.get("user") == "root"  # must chown as root (container's user)


def test_chown_passes_all_paths_through(monkeypatch):
    # The *paths splat must forward every path to a single chown -R invocation.
    monkeypatch.setattr(os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(os, "getgid", lambda: 2000, raising=False)
    c = MagicMock()
    c.exec_run.return_value = (0, b"")

    _chown_host_artifacts(c, ["/output", "/data/out"])

    args, _ = c.exec_run.call_args
    assert args[0] == ["chown", "-R", "1000:2000", "/output", "/data/out"]


def test_chown_is_noop_on_non_posix_host(monkeypatch):
    # No os.getuid (e.g. Windows dev host) -> leave container behavior unchanged.
    monkeypatch.delattr(os, "getuid", raising=False)
    c = MagicMock()
    _chown_host_artifacts(c, ["/output"])
    c.exec_run.assert_not_called()


def test_chown_is_noop_on_empty_paths(monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(os, "getgid", lambda: 1000, raising=False)
    c = MagicMock()
    _chown_host_artifacts(c, [])
    c.exec_run.assert_not_called()


def test_chown_swallows_exec_errors(monkeypatch):
    # A failed chown (e.g. read-only mount, container already gone) must never
    # break teardown — best-effort only.
    monkeypatch.setattr(os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(os, "getgid", lambda: 1000, raising=False)
    c = MagicMock()
    c.exec_run.side_effect = RuntimeError("container not running")
    _chown_host_artifacts(c, ["/output"])  # must not raise


def test_chown_tolerates_nonzero_exit(monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(os, "getgid", lambda: 1000, raising=False)
    c = MagicMock()
    c.exec_run.return_value = (1, b"chown: /output: Read-only file system")
    _chown_host_artifacts(c, ["/output"])  # logged, not raised


def test_chown_times_out_without_hanging(monkeypatch):
    # A wedged Docker daemon must not hang teardown: the bounded timeout fires,
    # the container is killed to unblock the worker, and the call returns.
    monkeypatch.setattr(os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(os, "getgid", lambda: 1000, raising=False)
    unblocked = threading.Event()
    c = MagicMock()
    c.exec_run.side_effect = lambda *a, **k: unblocked.wait(5)  # blocks until killed
    c.kill.side_effect = lambda: unblocked.set()  # real kill ends the in-flight exec

    _chown_host_artifacts(c, ["/output"], timeout=0.05)  # must return promptly

    c.kill.assert_called_once()
