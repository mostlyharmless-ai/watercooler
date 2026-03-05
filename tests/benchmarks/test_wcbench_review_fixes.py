from __future__ import annotations

import inspect
import json
import time
from pathlib import Path

import pytest

from tests.benchmarks.wcbench.config import RunConfig
from tests.benchmarks.wcbench.memory_seed import SeededEntry, _validate_seed_entries
from tests.benchmarks.wcbench.run_layout import make_run_layout
from tests.benchmarks.wcbench.tier_ablation import run_tier_ablation
from tests.benchmarks.wcbench.tracks import agent_value
from tests.benchmarks.wcbench import cli as wcbench_cli


@pytest.mark.benchmark
def test_exec_in_container_enforces_timeout() -> None:
    class _Result:
        def __init__(self) -> None:
            self.exit_code = 0
            self.output = (b"ok", b"")

    class _Container:
        def __init__(self) -> None:
            self.killed = False

        def exec_run(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            time.sleep(0.2)
            return _Result()

        def kill(self) -> None:
            self.killed = True

    container = _Container()
    exit_code, output = agent_value._exec_in_container(
        container,
        "sleep 999",
        timeout=0.01,
    )
    assert exit_code == 124
    assert "timeout" in output.lower()
    assert container.killed is True


@pytest.mark.benchmark
def test_clone_orphan_threads_reclones_existing_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "threads-clone"
    dest.mkdir(parents=True)
    (dest / "stale.txt").write_text("stale", encoding="utf-8")

    def _fake_clone(cmd: list[str], **_kwargs) -> None:  # type: ignore[no-untyped-def]
        assert cmd[0:2] == ["git", "clone"]
        target = Path(cmd[-1])
        target.mkdir(parents=True, exist_ok=True)
        (target / ".git").mkdir(parents=True, exist_ok=True)
        threads_dir = target / "threads"
        threads_dir.mkdir(parents=True, exist_ok=True)
        (threads_dir / "entry.md").write_text("answer leak", encoding="utf-8")
        (target / "graph").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(agent_value.subprocess, "run", _fake_clone)

    result = agent_value._clone_orphan_threads(
        repo_url="https://example.invalid/repo.git",
        branch="watercooler/threads",
        dest=dest,
    )
    assert result == dest
    assert not (dest / "stale.txt").exists()
    assert not (dest / ".git").exists()
    assert not (dest / "threads" / "entry.md").exists()
    assert (dest / "graph").exists()


@pytest.mark.benchmark
def test_validate_seed_entries_rejects_invalid_timestamp() -> None:
    with pytest.raises(ValueError, match="invalid timestamp"):
        _validate_seed_entries(
            [
                SeededEntry(
                    entry_id="E1",
                    title="bad ts",
                    body="Fact: something happened.",
                    timestamp="not-a-time",
                )
            ]
        )


@pytest.mark.benchmark
def test_validate_seed_entries_accepts_z_suffix_timestamp() -> None:
    _validate_seed_entries(
        [
            SeededEntry(
                entry_id="E2",
                title="good ts",
                body="Fact: something happened.",
                timestamp="2026-03-01T12:00:00Z",
            )
        ]
    )


@pytest.mark.benchmark
def test_tier_ablation_runs_via_orchestrator_and_uses_summary_elapsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def _fake_run_wcbench(cfg: RunConfig) -> None:
        calls.append(cfg.wc_tier_ceiling)
        layout = make_run_layout(tmp_path, cfg.run_id)
        payload = {
            "run_id": cfg.run_id,
            "track": cfg.track,
            "model": cfg.model,
            "mode": cfg.mode,
            "started_at": "2026-03-01T00:00:00Z",
            "ended_at": "2026-03-01T00:00:02Z",
            "elapsed_seconds": 2.0,
            "tasks": [
                {
                    "task_id": "q1",
                    "ok": True,
                    "mode": "memory_qa",
                    "category": "decision_recall",
                    "details": {},
                }
            ],
        }
        layout.summary_path.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr("tests.benchmarks.wcbench.tier_ablation.run_wcbench", _fake_run_wcbench)

    cfg = RunConfig(
        run_id="ablate-test",
        track="memory_qa",
        output_root=tmp_path,
    )
    table_path = run_tier_ablation(cfg, output_root=tmp_path)

    assert calls == ["T1", "T2", "T3"]
    assert table_path.exists()
    assert "decision_recall" in table_path.read_text(encoding="utf-8")

    t2_summary = make_run_layout(tmp_path, "ablate-test-T2").summary_path
    parsed = json.loads(t2_summary.read_text(encoding="utf-8"))
    assert parsed["elapsed_seconds"] == 2.0


@pytest.mark.benchmark
def test_agent_value_site_commit_flag_removed_from_cli_and_config() -> None:
    src = inspect.getsource(wcbench_cli.main)
    assert "--agent-value-site-commit" not in src
    with pytest.raises(TypeError):
        RunConfig(run_id="x", track="agent_value", agent_value_site_commit="main")
