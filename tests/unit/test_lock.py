from __future__ import annotations

import time
from pathlib import Path

from watercooler.lock import AdvisoryLock


def test_lock_acquire_release(tmp_path: Path):
    p = tmp_path / ".t.lock"
    with AdvisoryLock(p, timeout=1):
        assert p.exists()
    assert not p.exists()


def test_lock_timeout_then_force_break(tmp_path: Path):
    p = tmp_path / ".t.lock"
    # Acquire first
    l1 = AdvisoryLock(p, timeout=0, ttl=1)
    assert l1.acquire() is True
    try:
        l2 = AdvisoryLock(p, timeout=0.2, ttl=0)
        assert l2.acquire() is False
        # force break
        l3 = AdvisoryLock(p, timeout=0, ttl=0, force_break=True)
        assert l3.acquire() is True
        l3.release()
    finally:
        l1.release()

