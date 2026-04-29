"""argparse subcommand wiring for ``watercooler migrate``."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Optional


def add_migrate_parser(sub) -> None:
    """Register the ``migrate`` subcommand on the main argparse parser."""
    p = sub.add_parser(
        "migrate",
        help="Move T1/T2 memory between local (stdio) and hosted (hybrid).",
    )
    msub = p.add_subparsers(dest="migrate_tier", required=True)

    for tier_name in ("t1", "t2"):
        tier_parser = msub.add_parser(
            tier_name,
            help=f"Migrate {tier_name.upper()} memory between stdio and hybrid.",
        )
        tier_parser.add_argument(
            "--to",
            required=True,
            choices=["hybrid", "stdio"],
            help="Migration direction (target side).",
        )
        tier_parser.add_argument(
            "--code-path",
            default="",
            help="Repo root for canonical name resolution (default: cwd).",
        )
        tier_parser.add_argument(
            "--target-group-id",
            default="",
            help=(
                "Override canonical group_id (the FalkorDB database name "
                "is server-derived from group_id; not separately overridable)."
            ),
        )
        tier_parser.add_argument(
            "--local-host",
            default="localhost",
            help="Local FalkorDB host (default: localhost).",
        )
        tier_parser.add_argument(
            "--local-port",
            type=int,
            default=6379,
            help="Local FalkorDB port (default: 6379).",
        )
        tier_parser.add_argument(
            "--local-password",
            default="",
            help=(
                "Local FalkorDB password (default: read from "
                "FALKORDB_PASSWORD env var; empty if neither set). "
                "Prefer the env var to avoid leaking the password into "
                "shell history / `ps aux`."
            ),
        )
        tier_parser.add_argument(
            "--local-graph-name",
            default="",
            help="Override local FalkorDB graph name.",
        )
        tier_parser.add_argument(
            "--checkpoint",
            default="",
            help=(
                "Path to checkpoint file "
                "(default: ~/.watercooler/migration/<tier>_to_<direction>_cursor.jsonl, "
                "e.g. t1_to_hybrid_cursor.jsonl)."
            ),
        )
        tier_parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count what would happen, no writes.",
        )
        tier_parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Cap entries processed (0 = no cap).",
        )
        if tier_name == "t2":
            tier_parser.add_argument(
                "--threads",
                default="",
                help="Comma-separated thread topics to migrate (default: all).",
            )


def _resolve_local_password(args) -> Optional[str]:
    """CLI flag wins over env; return None if neither set."""
    import os
    cli_value = getattr(args, "local_password", "") or ""
    if cli_value:
        return cli_value
    env_value = os.environ.get("FALKORDB_PASSWORD", "")
    return env_value or None


def _dispatch(tier: str, direction: str, args) -> Any:
    """Pick + invoke the right migrate_* function. May raise on the
    inner functions' uncaught exceptions; cmd_migrate's try/except
    converts those to a JSON-summary contract."""
    if tier == "t1":
        from .t1 import migrate_t1_to_hybrid, migrate_t1_to_stdio
        kwargs: dict[str, Any] = dict(
            code_path=args.code_path or None,
            local_host=args.local_host,
            local_port=args.local_port,
            local_password=_resolve_local_password(args),
            local_graph_name=args.local_graph_name or None,
            target_group_id=args.target_group_id or None,
            checkpoint_path=Path(args.checkpoint).expanduser() if args.checkpoint else None,
            dry_run=args.dry_run,
            limit=args.limit,
        )
        if direction == "hybrid":
            return migrate_t1_to_hybrid(**kwargs)
        return migrate_t1_to_stdio(**kwargs)
    elif tier == "t2":
        from .t2 import migrate_t2_to_hybrid, migrate_t2_to_stdio
        kwargs = dict(
            code_path=args.code_path or None,
            target_group_id=args.target_group_id or None,
            dry_run=args.dry_run,
            limit=args.limit,
        )
        if direction == "hybrid":
            kwargs["threads_filter"] = args.threads
            return migrate_t2_to_hybrid(**kwargs)
        return migrate_t2_to_stdio(**kwargs)
    raise ValueError(f"Unknown tier: {tier}")


def cmd_migrate(args) -> int:
    """Dispatch ``watercooler migrate ...`` to the right tier+direction handler.

    The CLI honors a "stdout is JSON, stderr is logs, exit code reflects
    state" contract. Any uncaught exception from a migrate_* function
    (config error, network blip, programming bug) MUST be converted
    here into a populated MigrationSummary on stdout — never propagate
    as a Python traceback that would break automation parsing stdout
    for the JSON.
    """
    import logging as _logging
    from .summary import MigrationSummary

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    _logger = _logging.getLogger("watercooler.migration.cli")

    tier = args.migrate_tier
    direction = args.to

    try:
        summary = _dispatch(tier, direction, args)
    except Exception as e:
        # Boundary-of-CLI catch. Inner functions catch what they can
        # (specific FalkorDB / transport / iteration failures); this
        # is the catch-all that preserves the JSON-summary contract
        # even when the inner handlers miss something.
        direction_norm = (
            "stdio_to_hybrid" if direction == "hybrid" else "hybrid_to_stdio"
        )
        summary = MigrationSummary(
            tier=tier or "unknown",
            direction=direction_norm,
            dry_run=getattr(args, "dry_run", False),
        )
        summary.errored += 1
        summary.notes.append(
            f"Migration aborted unexpectedly: {type(e).__name__}: {e}. "
            "See stderr logs for full traceback. Re-run after resolving."
        )
        _logger.warning("migrate %s %s crashed: %s", tier, direction, e, exc_info=True)

    print(summary.to_json())
    return summary.exit_code()
