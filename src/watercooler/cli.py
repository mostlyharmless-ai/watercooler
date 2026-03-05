#!/usr/bin/env python3
"""Watercooler CLI - command-line interface for thread management."""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace

# Fail fast on unsupported interpreter version
if sys.version_info < (3, 10):
    print(f"Watercooler CLI requires Python 3.10+; found {sys.version.split()[0]}", file=sys.stderr)
    sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="watercooler",
        description="File-based collaboration for agentic coding",
    )

    sub = ap.add_subparsers(dest="cmd")

    # Command stubs
    p_init = sub.add_parser("init-thread", help="Initialize a new thread")
    p_init.add_argument("topic", help="Thread topic identifier")
    p_init.add_argument("--title", help="Optional title override")
    p_init.add_argument("--status", default="open", help="Initial status (default: open)")
    p_init.add_argument("--ball", default="codex", help="Initial ball owner (default: codex)")
    p_init.add_argument("--threads-dir", help="Threads directory (default: ./watercooler or $WATERCOOLER_DIR)")

    p_web = sub.add_parser("web-export", help="Generate HTML index")
    p_web.add_argument("--threads-dir")
    p_web.add_argument("--out", help="Optional output file path")
    p_web.add_argument("--open-only", action="store_true")
    p_web.add_argument("--closed", action="store_true")

    p_say = sub.add_parser("say", help="Quick team note with auto-ball-flip")
    p_say.add_argument("topic")
    p_say.add_argument("--threads-dir")
    p_say.add_argument("--agent", help="Agent name (defaults to Team)")
    p_say.add_argument("--role", help="Agent role (planner, critic, implementer, tester, pm, scribe)")
    p_say.add_argument("--title", required=True, help="Entry title")
    p_say.add_argument("--type", dest="entry_type", default="Note", help="Entry type (Note, Plan, Decision, PR, Closure)")
    p_say.add_argument("--body", required=True, help="Entry body text or @file path")
    p_say.add_argument("--status", help="Optional status update")
    p_say.add_argument("--ball", help="Optional ball update (auto-flips if not provided)")

    p_say.add_argument("--agents-file", help="Agent registry JSON file")

    p_ack = sub.add_parser("ack", help="Acknowledge without ball flip")
    p_ack.add_argument("topic")
    p_ack.add_argument("--threads-dir")
    p_ack.add_argument("--agent", help="Agent name (defaults to Team)")
    p_ack.add_argument("--role", help="Agent role (planner, critic, implementer, tester, pm, scribe)")
    p_ack.add_argument("--title", help="Entry title (default: Ack)")
    p_ack.add_argument("--type", dest="entry_type", default="Note", help="Entry type (Note, Plan, Decision, PR, Closure)")
    p_ack.add_argument("--body", help="Entry body text or @file path (default: ack)")
    p_ack.add_argument("--status", help="Optional status update")
    p_ack.add_argument("--ball", help="Optional ball update (does NOT auto-flip)")

    p_ack.add_argument("--agents-file", help="Agent registry JSON file")

    p_handoff = sub.add_parser("handoff", help="Flip ball to counterpart and append handoff entry")
    p_handoff.add_argument("topic")
    p_handoff.add_argument("--threads-dir")
    p_handoff.add_argument("--agent", help="Agent performing handoff (defaults to Team)")
    p_handoff.add_argument("--role", default="pm", help="Agent role (default: pm)")
    p_handoff.add_argument("--note", help="Optional custom handoff message")

    p_handoff.add_argument("--agents-file", help="Agent registry JSON file")

    p_list = sub.add_parser("list", help="List threads")
    p_list.add_argument("--threads-dir")
    p_list.add_argument("--open-only", action="store_true", help="Show only open threads")
    p_list.add_argument("--closed", action="store_true", help="Show only closed threads")

    p_reindex = sub.add_parser("reindex", help="Rebuild index")
    p_reindex.add_argument("--threads-dir")
    p_reindex.add_argument("--out", help="Optional output file path")
    p_reindex.add_argument("--open-only", action="store_true")
    p_reindex.add_argument("--closed", action="store_true")

    p_search = sub.add_parser("search", help="Search threads")
    p_search.add_argument("query")
    p_search.add_argument("--threads-dir")

    p_unlock = sub.add_parser("unlock", help="Clear advisory lock (debugging)")
    p_unlock.add_argument("topic")
    p_unlock.add_argument("--threads-dir")
    p_unlock.add_argument("--force", action="store_true", help="Remove lock even if active")

    p_check_branches = sub.add_parser("check-branches", help="Comprehensive audit of branch pairing")
    p_check_branches.add_argument("--code-root", help="Path to code repository (default: current directory)")
    p_check_branches.add_argument("--include-merged", action="store_true", help="Include fully merged branches")

    p_check_branch = sub.add_parser("check-branch", help="Validate branch pairing for specific branch")
    p_check_branch.add_argument("branch", help="Branch name to check")
    p_check_branch.add_argument("--code-root", help="Path to code repository (default: current directory)")

    p_merge_branch = sub.add_parser("merge-branch", help="Merge threads branch to main")
    p_merge_branch.add_argument("branch", help="Branch name to merge")
    p_merge_branch.add_argument("--code-root", help="Path to code repository (default: current directory)")
    p_merge_branch.add_argument("--force", action="store_true", help="Skip safety checks")

    p_archive_branch = sub.add_parser("archive-branch", help="Close OPEN threads, merge to main, then delete branch")
    p_archive_branch.add_argument("branch", help="Branch name to archive")
    p_archive_branch.add_argument("--code-root", help="Path to code repository (default: current directory)")
    p_archive_branch.add_argument("--abandon", action="store_true", help="Set OPEN threads to ABANDONED status")
    p_archive_branch.add_argument("--force", action="store_true", help="Skip confirmation prompts")

    p_install_hooks = sub.add_parser("install-hooks", help="Install git hooks for branch pairing validation")
    p_install_hooks.add_argument("--code-root", help="Path to code repository (default: current directory)")
    p_install_hooks.add_argument("--hooks-dir", help="Git hooks directory (default: .git/hooks)")
    p_install_hooks.add_argument("--force", action="store_true", help="Overwrite existing hooks")

    # Config commands
    p_config = sub.add_parser("config", help="Configuration management")
    config_sub = p_config.add_subparsers(dest="config_cmd")

    p_config_init = config_sub.add_parser("init", help="Initialize config file from template")
    p_config_init.add_argument("--user", action="store_true", help="Create user config (~/.watercooler/config.toml)")
    p_config_init.add_argument("--project", action="store_true", help="Create project config (.watercooler/config.toml)")
    p_config_init.add_argument("--force", action="store_true", help="Overwrite existing config")

    p_config_show = config_sub.add_parser("show", help="Show resolved configuration")
    p_config_show.add_argument("--project-path", help="Project directory for config discovery")
    p_config_show.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON")
    p_config_show.add_argument("--sources", action="store_true", help="Show config source files")

    p_config_validate = config_sub.add_parser("validate", help="Validate configuration files")
    p_config_validate.add_argument("--project-path", help="Project directory for config discovery")
    p_config_validate.add_argument("--strict", action="store_true", help="Treat warnings as errors")

    # Slack commands
    p_slack = sub.add_parser("slack", help="Slack integration")
    slack_sub = p_slack.add_subparsers(dest="slack_cmd")

    p_slack_setup = slack_sub.add_parser("setup", help="Interactive webhook setup")
    p_slack_setup.add_argument("--webhook-url", help="Webhook URL (skips interactive prompt)")

    p_slack_test = slack_sub.add_parser("test", help="Send a test notification")

    p_slack_status = slack_sub.add_parser("status", help="Show current Slack configuration")

    p_slack_disable = slack_sub.add_parser("disable", help="Disable Slack notifications")

    p_append = sub.add_parser("append-entry", help="Append a structured entry")
    p_append.add_argument("topic")
    p_append.add_argument("--threads-dir")
    p_append.add_argument("--agent", required=True, help="Agent name")
    p_append.add_argument("--role", required=True, help="Agent role (planner, critic, implementer, tester, pm, scribe)")
    p_append.add_argument("--title", required=True, help="Entry title")
    p_append.add_argument("--type", dest="entry_type", default="Note", help="Entry type (Note, Plan, Decision, PR, Closure)")
    p_append.add_argument("--body", required=True, help="Entry body text or @file path")
    p_append.add_argument("--status", help="Optional status update")
    p_append.add_argument("--ball", help="Optional ball update (auto-flips if not provided)")

    p_append.add_argument("--agents-file", help="Agent registry JSON file")

    p_set_status = sub.add_parser("set-status", help="Update thread status")
    p_set_status.add_argument("topic")
    p_set_status.add_argument("status")
    p_set_status.add_argument("--threads-dir")

    p_set_ball = sub.add_parser("set-ball", help="Update ball ownership")
    p_set_ball.add_argument("topic")
    p_set_ball.add_argument("ball")
    p_set_ball.add_argument("--threads-dir")

    p_sync = sub.add_parser("sync", help="Inspect or flush async git sync queue")
    p_sync.add_argument("--code-path", help="Code repository root (default: current directory)")
    p_sync.add_argument("--threads-dir", help="Threads directory override")
    p_sync.add_argument("--status", action="store_true", help="Show pending queue status without flushing")
    p_sync.add_argument("--now", action="store_true", help="Force an immediate push of pending commits")

    # Memory graph commands
    p_memory = sub.add_parser("memory", help="Memory graph operations")
    memory_sub = p_memory.add_subparsers(dest="memory_cmd")

    p_memory_build = memory_sub.add_parser("build", help="Build memory graph from threads")
    p_memory_build.add_argument("--threads-dir", help="Threads directory (default: ./watercooler)")
    p_memory_build.add_argument("--output", "-o", help="Output file path for graph JSON")
    p_memory_build.add_argument("--no-summaries", action="store_true", help="Skip summary generation")
    p_memory_build.add_argument("--no-embeddings", action="store_true", help="Skip embedding generation")
    p_memory_build.add_argument("--branch", help="Git branch context")

    p_memory_export = memory_sub.add_parser("export", help="Export graph to external format")
    p_memory_export.add_argument("--graph", help="Input graph JSON (builds from threads if not provided)")
    p_memory_export.add_argument("--threads-dir", help="Threads directory (if building)")
    p_memory_export.add_argument("--format", choices=["leanrag", "json"], default="leanrag", help="Export format")
    p_memory_export.add_argument("--output", "-o", required=True, help="Output path (directory for leanrag, file for json)")
    p_memory_export.add_argument("--no-embeddings", action="store_true", help="Exclude embeddings from export")

    p_memory_stats = memory_sub.add_parser("stats", help="Show graph statistics")
    p_memory_stats.add_argument("--graph", help="Graph JSON file (builds from threads if not provided)")
    p_memory_stats.add_argument("--threads-dir", help="Threads directory (if building)")

    # Baseline graph commands (free-tier, local LLM)
    p_baseline = sub.add_parser("baseline-graph", help="Baseline graph operations (free-tier, local LLM)")
    baseline_sub = p_baseline.add_subparsers(dest="baseline_cmd")

    p_baseline_build = baseline_sub.add_parser("build", help="Build baseline graph from threads")
    p_baseline_build.add_argument("--threads-dir", help="Threads directory (default: ./watercooler)")
    p_baseline_build.add_argument("--output", "-o", help="Output directory for graph files")
    p_baseline_build.add_argument("--extractive-only", action="store_true", help="Use extractive summaries only (no LLM)")
    p_baseline_build.add_argument("--skip-closed", action="store_true", help="Skip closed threads")

    p_baseline_stats = baseline_sub.add_parser("stats", help="Show threads statistics")
    p_baseline_stats.add_argument("--threads-dir", help="Threads directory")

    args = ap.parse_args(argv)

    if not args.cmd:
        ap.print_help()
        sys.exit(0)

    if args.cmd == "init-thread":
        from pathlib import Path
        from .path_resolver import resolve_threads_dir
        from .commands_graph import init_thread

        out = init_thread(
            args.topic,
            threads_dir=resolve_threads_dir(args.threads_dir),
            title=args.title,
            status=args.status,
            ball=args.ball,
        )
        print(str(out))
        sys.exit(0)

    if args.cmd == "append-entry":
        from pathlib import Path
        from ulid import ULID
        from .fs import read_body
        from .path_resolver import resolve_threads_dir
        from .commands_graph import append_entry
        from .agents import _load_agents_registry

        body = read_body(args.body)
        registry = _load_agents_registry(args.agents_file) if hasattr(args, 'agents_file') and args.agents_file else None
        out = append_entry(
            args.topic,
            threads_dir=resolve_threads_dir(args.threads_dir),
            agent=args.agent,
            role=args.role,
            title=args.title,
            entry_type=args.entry_type,
            body=body,
            status=args.status,
            ball=args.ball,
            registry=registry,
            entry_id=str(ULID()),
        )
        print(str(out))
        sys.exit(0)

    if args.cmd == "set-status":
        from pathlib import Path
        from .commands_graph import set_status
        from .path_resolver import resolve_threads_dir

        out = set_status(args.topic, threads_dir=resolve_threads_dir(args.threads_dir), status=args.status)
        print(str(out))
        sys.exit(0)

    if args.cmd == "set-ball":
        from pathlib import Path
        from .commands_graph import set_ball
        from .path_resolver import resolve_threads_dir

        out = set_ball(args.topic, threads_dir=resolve_threads_dir(args.threads_dir), ball=args.ball)
        print(str(out))
        sys.exit(0)

    if args.cmd == "sync":
        print(
            "The 'sync' command has been removed. "
            "Thread sync is now handled automatically via the orphan branch worktree.",
            file=sys.stderr,
        )
        sys.exit(0)

        if args.status:
            _print_status(sync.get_async_status())
        sys.exit(0)

    if args.cmd == "list":
        from pathlib import Path
        from .commands import list_threads
        from .path_resolver import resolve_threads_dir

        oo: bool | None = None
        if args.open_only and args.closed:
            oo = None
        elif args.open_only:
            oo = True
        elif args.closed:
            oo = False
        rows = list_threads(threads_dir=resolve_threads_dir(args.threads_dir), open_only=oo)
        for title, status, ball, updated, path, is_new in rows:
            newcol = "NEW" if is_new else ""
            print(f"{updated}\t{status}\t{ball}\t{newcol}\t{title}\t{path}")
        sys.exit(0)

    if args.cmd == "reindex":
        from pathlib import Path
        from .commands import reindex
        from .path_resolver import resolve_threads_dir

        oo: bool | None = True
        if args.open_only and args.closed:
            oo = None
        elif args.closed:
            oo = False
        elif args.open_only:
            oo = True
        out = reindex(threads_dir=resolve_threads_dir(args.threads_dir), out_file=Path(args.out) if args.out else None, open_only=oo)
        print(str(out))
        sys.exit(0)

    if args.cmd == "search":
        from pathlib import Path
        from .commands import search
        from .path_resolver import resolve_threads_dir

        hits = search(threads_dir=resolve_threads_dir(args.threads_dir), query=args.query)
        for p, ln, line in hits:
            print(f"{p}:{ln}: {line}")
        sys.exit(0)

    if args.cmd == "unlock":
        from pathlib import Path
        from .commands import unlock
        from .path_resolver import resolve_threads_dir

        unlock(
            args.topic,
            threads_dir=resolve_threads_dir(args.threads_dir),
            force=args.force
        )
        sys.exit(0)

    if args.cmd == "web-export":
        from pathlib import Path
        from .commands import web_export
        from .path_resolver import resolve_threads_dir

        oo: bool | None = True
        if args.open_only and args.closed:
            oo = None
        elif args.closed:
            oo = False
        elif args.open_only:
            oo = True
        out = web_export(threads_dir=resolve_threads_dir(args.threads_dir), out_file=Path(args.out) if args.out else None, open_only=oo)
        print(str(out))
        sys.exit(0)

    if args.cmd == "say":
        from pathlib import Path
        from ulid import ULID
        from .fs import read_body
        from .path_resolver import resolve_threads_dir
        from .commands_graph import say
        from .agents import _load_agents_registry

        body = read_body(args.body)
        registry = _load_agents_registry(args.agents_file) if hasattr(args, 'agents_file') and args.agents_file else None
        out = say(
            args.topic,
            threads_dir=resolve_threads_dir(args.threads_dir),
            agent=args.agent,
            role=args.role,
            title=args.title,
            entry_type=args.entry_type,
            body=body,
            status=args.status,
            ball=args.ball,
            registry=registry,
            entry_id=str(ULID()),
        )
        print(str(out))
        sys.exit(0)

    if args.cmd == "ack":
        from pathlib import Path
        from ulid import ULID
        from .fs import read_body
        from .commands_graph import ack
        from .path_resolver import resolve_threads_dir
        from .agents import _load_agents_registry

        body = read_body(args.body) if args.body else None
        registry = _load_agents_registry(args.agents_file) if hasattr(args, 'agents_file') and args.agents_file else None
        out = ack(
            args.topic,
            threads_dir=resolve_threads_dir(args.threads_dir),
            agent=args.agent,
            role=args.role,
            title=args.title,
            entry_type=args.entry_type,
            body=body,
            status=args.status,
            ball=args.ball,
            registry=registry,
            entry_id=str(ULID()),
        )
        print(str(out))
        sys.exit(0)

    if args.cmd == "handoff":
        from pathlib import Path
        from ulid import ULID
        from .commands_graph import handoff
        from .path_resolver import resolve_threads_dir
        from .agents import _load_agents_registry

        registry = _load_agents_registry(args.agents_file) if hasattr(args, 'agents_file') and args.agents_file else None
        out = handoff(
            args.topic,
            threads_dir=resolve_threads_dir(args.threads_dir),
            agent=args.agent,
            role=args.role,
            note=args.note,
            registry=registry,
            entry_id=str(ULID()),
        )
        print(str(out))
        sys.exit(0)

    if args.cmd == "check-branches":
        from pathlib import Path
        from .commands import check_branches

        code_root = Path(args.code_root).resolve() if args.code_root else None
        result = check_branches(code_root=code_root, include_merged=args.include_merged)
        print(result)
        sys.exit(0)

    if args.cmd == "check-branch":
        from pathlib import Path
        from .commands import check_branch

        code_root = Path(args.code_root).resolve() if args.code_root else None
        result = check_branch(args.branch, code_root=code_root)
        print(result)
        sys.exit(0)

    if args.cmd == "merge-branch":
        from pathlib import Path
        from .commands import merge_branch

        code_root = Path(args.code_root).resolve() if args.code_root else None
        result = merge_branch(args.branch, code_root=code_root, force=args.force)
        print(result)
        sys.exit(0)

    if args.cmd == "archive-branch":
        from pathlib import Path
        from .commands import archive_branch

        code_root = Path(args.code_root).resolve() if args.code_root else None
        result = archive_branch(args.branch, code_root=code_root, abandon=args.abandon, force=args.force)
        print(result)
        sys.exit(0)

    if args.cmd == "install-hooks":
        from pathlib import Path
        from .commands import install_hooks

        code_root = Path(args.code_root).resolve() if args.code_root else None
        hooks_dir = Path(args.hooks_dir).resolve() if args.hooks_dir else None
        result = install_hooks(code_root=code_root, hooks_dir=hooks_dir, force=args.force)
        print(result)
        sys.exit(0)

    if args.cmd == "config":
        from pathlib import Path
        import json as json_module
        import shutil

        if not args.config_cmd:
            print("Usage: watercooler config {init|show|validate}")
            sys.exit(0)

        if args.config_cmd == "init":
            from .config_loader import ensure_config_dir, CONFIG_FILENAME

            # Get template path
            template_path = Path(__file__).parent / "templates" / "config.example.toml"
            if not template_path.exists():
                print(f"❌ Template not found: {template_path}", file=sys.stderr)
                sys.exit(1)

            # Determine target (default to user config)
            if args.project:
                config_dir = ensure_config_dir(user=False, project_path=Path.cwd())
                target_path = config_dir / CONFIG_FILENAME
                location = "project"
            else:
                config_dir = ensure_config_dir(user=True)
                target_path = config_dir / CONFIG_FILENAME
                location = "user"

            if target_path.exists() and not args.force:
                print(f"❌ Config already exists: {target_path}", file=sys.stderr)
                print("Use --force to overwrite.", file=sys.stderr)
                sys.exit(1)

            shutil.copy(template_path, target_path)
            print(f"✅ Created {location} config: {target_path}")
            print(f"   Edit this file to customize Watercooler settings.")
            sys.exit(0)

        if args.config_cmd == "show":
            from .config_loader import load_config, get_config_paths, ConfigError

            project_path = Path(args.project_path) if args.project_path else None

            if args.sources:
                paths = get_config_paths(project_path)
                print("Config sources (in priority order):")
                print()
                for name, path in paths.items():
                    if path and path.exists():
                        print(f"  ✓ {name}: {path}")
                    elif path:
                        print(f"  ✗ {name}: {path} (not found)")
                    else:
                        print(f"  - {name}: (not applicable)")
                print()
                print("Environment variables override all file configs.")
                sys.exit(0)

            try:
                config = load_config(project_path)
            except ConfigError as e:
                print(f"❌ Config error: {e}", file=sys.stderr)
                sys.exit(1)

            if args.as_json:
                print(json_module.dumps(config.model_dump(), indent=2))
            else:
                # Use tomlkit for proper TOML output that stays in sync with schema
                try:
                    import tomlkit
                    doc = tomlkit.document()
                    doc.add(tomlkit.comment(" Watercooler Configuration (resolved)"))
                    doc.add(tomlkit.nl())

                    # Convert Pydantic model to dict, using by_alias for 'async' field
                    config_dict = config.model_dump(by_alias=True)
                    for section, values in config_dict.items():
                        if isinstance(values, dict):
                            table = tomlkit.table()
                            for key, val in values.items():
                                if isinstance(val, dict):
                                    # Nested table (e.g., mcp.git, mcp.sync)
                                    subtable = tomlkit.table()
                                    for subkey, subval in val.items():
                                        subtable.add(subkey, subval)
                                    table.add(key, subtable)
                                else:
                                    table.add(key, val)
                            doc.add(section, table)
                        else:
                            doc.add(section, values)

                    print(tomlkit.dumps(doc))
                except ImportError:
                    # Fallback if tomlkit not installed
                    print("# Watercooler Configuration (resolved)")
                    print("# Note: Install tomlkit for proper TOML formatting")
                    print()
                    print(json_module.dumps(config.model_dump(), indent=2))

            sys.exit(0)

        if args.config_cmd == "validate":
            from .config_loader import load_config, get_config_paths, ConfigError

            project_path = Path(args.project_path) if args.project_path else None
            paths = get_config_paths(project_path)

            errors = []
            warnings = []

            # Check which configs exist
            found_any = False
            for name, path in paths.items():
                if path and path.exists():
                    found_any = True
                    print(f"  ✓ Found: {path}")

            if not found_any:
                warnings.append("No config files found. Using defaults.")

            # Try to load and validate
            try:
                config = load_config(project_path)
                print()
                print("✓ Configuration is valid.")

                # Check for potential issues
                if config.mcp.transport == "http" and config.mcp.port < 1024:
                    warnings.append(f"Port {config.mcp.port} requires root privileges.")

                if config.validation.fail_on_violation:
                    warnings.append("fail_on_violation=true: Invalid entries will cause errors.")

            except ConfigError as e:
                errors.append(str(e))

            # Report
            if warnings:
                print()
                print("Warnings:")
                for w in warnings:
                    print(f"  ⚠ {w}")

            if errors:
                print()
                print("Errors:", file=sys.stderr)
                for e in errors:
                    print(f"  ❌ {e}", file=sys.stderr)
                sys.exit(1)

            if args.strict and warnings:
                print()
                print("--strict: Treating warnings as errors.", file=sys.stderr)
                sys.exit(1)

            sys.exit(0)

    if args.cmd == "slack":
        from .slack_cli import slack_setup, slack_test, slack_status, slack_disable

        if not args.slack_cmd:
            print("Usage: watercooler slack {setup|test|status|disable}")
            sys.exit(0)

        if args.slack_cmd == "setup":
            code = slack_setup(webhook_url=args.webhook_url if hasattr(args, 'webhook_url') else None)
            sys.exit(code)

        if args.slack_cmd == "test":
            code = slack_test()
            sys.exit(code)

        if args.slack_cmd == "status":
            code = slack_status()
            sys.exit(code)

        if args.slack_cmd == "disable":
            code = slack_disable()
            sys.exit(code)

    if args.cmd == "memory":
        from pathlib import Path
        from .path_resolver import resolve_threads_dir

        if not args.memory_cmd:
            print("Usage: watercooler memory {build|export|stats}")
            sys.exit(0)

        if args.memory_cmd == "build":
            from watercooler_memory import MemoryGraph, GraphConfig

            threads_dir = resolve_threads_dir(args.threads_dir)
            if not threads_dir.exists():
                print(f"❌ Threads directory not found: {threads_dir}", file=sys.stderr)
                sys.exit(1)

            config = GraphConfig(
                generate_summaries=not args.no_summaries,
                generate_embeddings=not args.no_embeddings,
            )

            print(f"Building memory graph from {threads_dir}...")
            graph = MemoryGraph(config)

            try:
                graph.build(threads_dir, branch_context=args.branch)
            except ImportError as e:
                print(f"⚠ Missing dependency: {e}", file=sys.stderr)
                print("Install with: pip install 'watercooler-cloud[memory]'", file=sys.stderr)
                # Continue with partial build
            except Exception as e:
                print(f"❌ Build error: {e}", file=sys.stderr)
                sys.exit(1)

            stats = graph.stats()
            print(f"✅ Built graph: {stats['threads']} threads, {stats['entries']} entries, {stats['chunks']} chunks")

            if args.output:
                output_path = Path(args.output)
                graph.save(output_path)
                print(f"   Saved to: {output_path}")

            sys.exit(0)

        if args.memory_cmd == "export":
            from watercooler_memory import MemoryGraph, GraphConfig
            from watercooler_memory.leanrag_export import export_to_leanrag

            # Load or build graph
            if args.graph:
                graph_path = Path(args.graph)
                if not graph_path.exists():
                    print(f"❌ Graph file not found: {graph_path}", file=sys.stderr)
                    sys.exit(1)
                graph = MemoryGraph.load(graph_path)
            else:
                threads_dir = resolve_threads_dir(args.threads_dir)
                if not threads_dir.exists():
                    print(f"❌ Threads directory not found: {threads_dir}", file=sys.stderr)
                    sys.exit(1)

                print(f"Building graph from {threads_dir}...")
                config = GraphConfig(
                    generate_summaries=True,
                    generate_embeddings=not args.no_embeddings,
                )
                graph = MemoryGraph(config)
                try:
                    graph.build(threads_dir)
                except ImportError as e:
                    print(f"⚠ Missing dependency: {e}", file=sys.stderr)
                except Exception as e:
                    print(f"❌ Build error: {e}", file=sys.stderr)
                    sys.exit(1)

            output_path = Path(args.output)

            if args.format == "leanrag":
                manifest = export_to_leanrag(
                    graph, output_path, include_embeddings=not args.no_embeddings
                )
                print(f"✅ Exported to LeanRAG format: {output_path}")
                print(f"   {manifest['statistics']['documents']} documents, {manifest['statistics']['chunks']} chunks")
            else:
                graph.save(output_path)
                print(f"✅ Saved graph JSON: {output_path}")

            sys.exit(0)

        if args.memory_cmd == "stats":
            from watercooler_memory import MemoryGraph, GraphConfig

            # Load or build graph
            if args.graph:
                graph_path = Path(args.graph)
                if not graph_path.exists():
                    print(f"❌ Graph file not found: {graph_path}", file=sys.stderr)
                    sys.exit(1)
                graph = MemoryGraph.load(graph_path)
            else:
                threads_dir = resolve_threads_dir(args.threads_dir)
                if not threads_dir.exists():
                    print(f"❌ Threads directory not found: {threads_dir}", file=sys.stderr)
                    sys.exit(1)

                config = GraphConfig(
                    generate_summaries=False,
                    generate_embeddings=False,
                )
                graph = MemoryGraph(config)
                graph.build(threads_dir)

            stats = graph.stats()
            print("Memory Graph Statistics:")
            print(f"  Threads:              {stats['threads']}")
            print(f"  Entries:              {stats['entries']}")
            print(f"  Chunks:               {stats['chunks']}")
            print(f"  Edges:                {stats['edges']}")
            print(f"  Hyperedges:           {stats['hyperedges']}")
            print(f"  Entries w/summaries:  {stats['entries_with_summaries']}")
            print(f"  Entries w/embeddings: {stats['entries_with_embeddings']}")
            print(f"  Chunks w/embeddings:  {stats['chunks_with_embeddings']}")
            sys.exit(0)

    if args.cmd == "baseline-graph":
        from pathlib import Path
        from .path_resolver import resolve_threads_dir

        if not args.baseline_cmd:
            print("Usage: watercooler baseline-graph {build|stats}")
            sys.exit(0)

        if args.baseline_cmd == "build":
            from .baseline_graph import export_all_threads, SummarizerConfig

            threads_dir = resolve_threads_dir(args.threads_dir)
            if not threads_dir.exists():
                print(f"Threads directory not found: {threads_dir}", file=sys.stderr)
                sys.exit(1)

            # Default output to threads_dir/graph/baseline
            if args.output:
                output_dir = Path(args.output)
            else:
                output_dir = threads_dir / "graph" / "baseline"

            config = SummarizerConfig(prefer_extractive=args.extractive_only)

            print(f"Building baseline graph from {threads_dir}...")
            if args.extractive_only:
                print("  Mode: extractive only (no LLM)")
            else:
                print(f"  Mode: LLM ({config.api_base})")
            if args.skip_closed:
                print("  Skipping closed threads")

            manifest = export_all_threads(
                threads_dir, output_dir, config, skip_closed=args.skip_closed
            )

            print()
            print(f"Baseline graph built: {output_dir}")
            print(f"  Threads: {manifest['threads_exported']}")
            print(f"  Entries: {manifest['entries_exported']}")
            print(f"  Nodes:   {manifest['nodes_written']}")
            print(f"  Edges:   {manifest['edges_written']}")
            sys.exit(0)

        if args.baseline_cmd == "stats":
            from .baseline_graph import get_thread_stats

            threads_dir = resolve_threads_dir(args.threads_dir)
            if not threads_dir.exists():
                print(f"Threads directory not found: {threads_dir}", file=sys.stderr)
                sys.exit(1)

            stats = get_thread_stats(threads_dir)
            print("Baseline Graph Statistics:")
            print(f"  Threads dir:          {stats['threads_dir']}")
            print(f"  Total threads:        {stats['total_threads']}")
            print(f"  Total entries:        {stats['total_entries']}")
            print(f"  Avg entries/thread:   {stats['avg_entries_per_thread']:.1f}")
            print()
            print("  Status breakdown:")
            for status, count in stats.get('status_breakdown', {}).items():
                print(f"    {status}: {count}")
            sys.exit(0)

    # default: other commands not yet implemented in L1
    print(f"watercooler {args.cmd}: not yet implemented (L1 stub)")
    sys.exit(0)


if __name__ == "__main__":
    main()
