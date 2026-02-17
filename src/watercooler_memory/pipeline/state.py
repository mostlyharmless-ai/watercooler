"""Pipeline state management for resumable processing."""

from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Optional, Any
import json

from .._utils import _utc_now_iso as _utc_iso_timestamp


class StageStatus(str, Enum):
    """Status of a pipeline stage."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class Stage(str, Enum):
    """Pipeline stages in order."""

    EXPORT = "export"  # threads → LeanRAG format
    EXTRACT = "extract"  # entity/relation extraction
    DEDUPE = "dedupe"  # deduplicate entities
    BUILD = "build"  # build knowledge graph
    QUERY = "query"  # (optional) query interface

    @classmethod
    def ordered(cls) -> list["Stage"]:
        """Return stages in execution order."""
        return [cls.EXPORT, cls.EXTRACT, cls.DEDUPE, cls.BUILD]


@dataclass
class StageState:
    """State of a single stage."""

    status: StageStatus = StageStatus.PENDING
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None

    # Progress tracking
    total_items: int = 0
    processed_items: int = 0
    failed_items: int = 0

    # Batch tracking for resumable processing
    current_batch: int = 0
    total_batches: int = 0

    # Stage-specific outputs
    outputs: dict[str, Any] = field(default_factory=dict)

    @property
    def progress_pct(self) -> float:
        """Progress percentage."""
        if self.total_items == 0:
            return 0.0
        return (self.processed_items / self.total_items) * 100

    def start(self, total_items: int = 0) -> None:
        """Mark stage as started."""
        self.status = StageStatus.RUNNING
        self.started_at = _utc_iso_timestamp()
        self.total_items = total_items
        self.processed_items = 0
        self.failed_items = 0
        self.error = None

    def complete(self) -> None:
        """Mark stage as completed."""
        self.status = StageStatus.COMPLETED
        self.completed_at = _utc_iso_timestamp()

    def fail(self, error: str) -> None:
        """Mark stage as failed."""
        self.status = StageStatus.FAILED
        self.completed_at = _utc_iso_timestamp()
        self.error = error

    def update_progress(self, processed: int, failed: int = 0) -> None:
        """Update progress counters."""
        self.processed_items = processed
        self.failed_items = failed


@dataclass
class PipelineState:
    """Full pipeline state."""

    # Run metadata
    run_id: str = ""
    created_at: str = ""
    updated_at: str = ""

    # Source info
    threads_dir: str = ""
    work_dir: str = ""

    # Test mode
    test_mode: bool = False

    # Stage states
    stages: dict[str, StageState] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Initialize stage states if empty."""
        if not self.stages:
            self.stages = {stage.value: StageState() for stage in Stage.ordered()}

    def get_stage(self, stage: Stage) -> StageState:
        """Get state for a specific stage."""
        if stage.value not in self.stages:
            self.stages[stage.value] = StageState()
        return self.stages[stage.value]

    def current_stage(self) -> Optional[Stage]:
        """Get the current (running or next pending) stage."""
        for stage in Stage.ordered():
            state = self.get_stage(stage)
            if state.status == StageStatus.RUNNING:
                return stage
            if state.status == StageStatus.PENDING:
                return stage
        return None

    def is_complete(self) -> bool:
        """Check if all stages are complete."""
        return all(self.get_stage(s).status == StageStatus.COMPLETED for s in Stage.ordered())

    def can_run_stage(self, stage: Stage) -> tuple[bool, str]:
        """Check if a stage can be run.

        Returns:
            Tuple of (can_run, reason)
        """
        stages = Stage.ordered()
        idx = stages.index(stage)

        # Check all previous stages are complete
        for prev_stage in stages[:idx]:
            state = self.get_stage(prev_stage)
            if state.status != StageStatus.COMPLETED:
                return False, f"Previous stage '{prev_stage.value}' not completed (status: {state.status.value})"

        # Check this stage isn't already running
        state = self.get_stage(stage)
        if state.status == StageStatus.RUNNING:
            return False, f"Stage '{stage.value}' is already running"

        return True, "ok"

    def save(self, path: Path) -> None:
        """Save state to file."""
        self.updated_at = _utc_iso_timestamp()

        # Convert to dict, handling StageState objects
        data = {
            "run_id": self.run_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "threads_dir": self.threads_dir,
            "work_dir": self.work_dir,
            "test_mode": self.test_mode,
            "stages": {name: asdict(state) for name, state in self.stages.items()},
        }

        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    @classmethod
    def load(cls, path: Path) -> "PipelineState":
        """Load state from file."""
        with open(path) as f:
            data = json.load(f)

        # Reconstruct StageState objects
        stages = {}
        for name, state_data in data.get("stages", {}).items():
            state_data["status"] = StageStatus(state_data["status"])
            stages[name] = StageState(**state_data)

        return cls(
            run_id=data["run_id"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            threads_dir=data["threads_dir"],
            work_dir=data["work_dir"],
            test_mode=data.get("test_mode", False),
            stages=stages,
        )

    @classmethod
    def create(cls, run_id: str, threads_dir: Path, work_dir: Path, test_mode: bool = False) -> "PipelineState":
        """Create a new pipeline state."""
        now = _utc_iso_timestamp()
        return cls(
            run_id=run_id,
            created_at=now,
            updated_at=now,
            threads_dir=str(threads_dir),
            work_dir=str(work_dir),
            test_mode=test_mode,
        )


def get_state_path(work_dir: Path, run_id: str) -> Path:
    """Get path to state file for a run."""
    return work_dir / "state" / f"{run_id}.json"


def list_runs(work_dir: Path) -> list[str]:
    """List all run IDs in work directory."""
    state_dir = work_dir / "state"
    if not state_dir.exists():
        return []
    return [p.stem for p in sorted(state_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)]


def load_or_create_state(work_dir: Path, run_id: str, threads_dir: Path, test_mode: bool = False) -> PipelineState:
    """Load existing state or create new one."""
    state_path = get_state_path(work_dir, run_id)
    if state_path.exists():
        return PipelineState.load(state_path)
    return PipelineState.create(run_id, threads_dir, work_dir, test_mode)
