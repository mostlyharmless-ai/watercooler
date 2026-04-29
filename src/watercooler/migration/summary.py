"""Result type for migration operations."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class MigrationSummary:
    """Final outcome of a migrate-XXX call.

    Always fully populated even on partial failure so the CLI can surface
    a useful one-line health summary plus the counts that matter.

    State signals (mutually exclusive sources of non-success):
      - ``errored > 0``  : real failure (per-entry upsert / generation error)
      - ``not_implemented = True`` : the requested direction is intentionally
        unimplemented in this release (e.g. T2 hybrid→stdio); see ``notes``
        for the canonical workaround. Distinct from ``errored`` so scripts
        can tell "real failure" apart from "feature deferred."

    The CLI maps these to distinct exit codes:
      - 0  : clean success (or clean dry-run)
      - 2  : ``errored > 0``
      - 64 : ``not_implemented`` (sysexits.h EX_USAGE — "the command was used
             incorrectly," which fits "this command path isn't built yet")
    """

    tier: str  # "t1" or "t2"
    direction: str  # "stdio_to_hybrid" or "hybrid_to_stdio"
    dry_run: bool
    total_scanned: int = 0
    pushed: int = 0
    skipped_already_present: int = 0
    cache_hits: int = 0
    api_calls: int = 0
    errored: int = 0
    not_implemented: bool = False
    elapsed_seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)

    def is_clean(self) -> bool:
        return self.errored == 0 and not self.not_implemented

    def exit_code(self) -> int:
        """CLI exit code corresponding to the summary state."""
        if self.not_implemented:
            return 64  # EX_USAGE — feature path not yet built
        if self.errored > 0:
            return 2
        return 0
