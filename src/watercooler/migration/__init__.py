"""Watercooler memory-tier migration: stdio ↔ hybrid.

Provides the ``watercooler migrate`` CLI for moving T1 (entry embeddings)
and T2 (Graphiti episodes/entities) between local FalkorDB (stdio mode)
and the hosted FalkorDB (hybrid mode).

Customer use case: a user transitions from local-only ``stdio`` to
``hybrid`` (or back) and wants their accumulated memory to come along
without manual reindexing.

CLI surface:

    watercooler migrate t1 --to hybrid [--dry-run] [--limit N]
    watercooler migrate t1 --to stdio  [--dry-run] [--limit N]
    watercooler migrate t2 --to hybrid [--dry-run] [--limit N]
    watercooler migrate t2 --to stdio  [--dry-run] [--limit N]
"""

from __future__ import annotations

from .checkpoint import Checkpoint
from .summary import MigrationSummary
from .t1 import migrate_t1_to_hybrid, migrate_t1_to_stdio
from .t2 import migrate_t2_to_hybrid, migrate_t2_to_stdio

__all__ = [
    "Checkpoint",
    "MigrationSummary",
    "migrate_t1_to_hybrid",
    "migrate_t1_to_stdio",
    "migrate_t2_to_hybrid",
    "migrate_t2_to_stdio",
]
