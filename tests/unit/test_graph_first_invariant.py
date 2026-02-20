"""CI guard: ensure no runtime code imports deprecated .md-parsing functions.

Graph is the sole source of truth for all read operations. These functions
are deprecated and only allowed in:
- baseline_graph/parser.py (graph rebuild from .md)
- hosted_ops.py (hosted reconciliation)
- tests/
- scripts/
"""

import re
from pathlib import Path

import pytest

# Root of the source tree
SRC_ROOT = Path(__file__).resolve().parents[2] / "src"

# Deprecated function names that should not appear in runtime imports
_DEPRECATED_FUNCTIONS = {
    "discover_thread_files",
    "parse_thread_entries",
    "parse_thread_header",
    "parse_thread_file",
}

# Files that are explicitly allowed to import these (graph rebuild / reconciliation)
_ALLOWED_FILES = {
    # Graph rebuild from .md (sync_thread_to_graph)
    "watercooler/baseline_graph/parser.py",
    "watercooler/baseline_graph/sync.py",
    "watercooler/baseline_graph/__init__.py",
    # Hosted reconciliation (reconcile_thread_hosted)
    "watercooler_mcp/hosted_ops.py",
    # The functions' own definitions
    "watercooler/thread_entries.py",
    "watercooler/fs.py",
}

# Import pattern: "from ... import ... <name>" or "import ... <name>"
_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+\S+\s+import\s+.*|import\s+.*)"
)


def _scan_for_deprecated_imports() -> list[str]:
    """Scan src/ for runtime files that import deprecated functions.

    Handles both single-line and multi-line parenthesized imports by
    scanning the full file content for deprecated names within import
    blocks. Uses word-boundary matching to avoid false positives on
    names that are substrings of longer identifiers.
    """
    violations = []

    for py_file in SRC_ROOT.rglob("*.py"):
        # Get path relative to src/
        rel = py_file.relative_to(SRC_ROOT)
        rel_str = str(rel)

        # Skip allowed files
        if rel_str in _ALLOWED_FILES:
            continue

        try:
            content = py_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        # Extract all import blocks (handles multi-line parenthesized imports)
        # Join continuation lines for multi-line imports, then check
        lines = content.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            line_no = i + 1

            if not _IMPORT_RE.match(line):
                i += 1
                continue

            # Accumulate multi-line import (parenthesized)
            import_block = line
            if "(" in line and ")" not in line:
                j = i + 1
                while j < len(lines):
                    import_block += " " + lines[j]
                    if ")" in lines[j]:
                        break
                    j += 1

            for func_name in _DEPRECATED_FUNCTIONS:
                if re.search(rf"\b{re.escape(func_name)}\b", import_block):
                    violations.append(
                        f"{rel_str}:{line_no}: imports deprecated "
                        f"'{func_name}' — use graph reader instead"
                    )

            i += 1

    return violations


# Write commands that must come from commands_graph, not commands
_GRAPH_WRITE_COMMANDS = {"init_thread", "append_entry", "say", "ack", "handoff", "set_status", "set_ball"}

# Pattern: "from .commands import <write_command>" (not commands_graph)
_LEGACY_CMD_RE = re.compile(
    r"^\s*from\s+\.commands\s+import\s+(.+)"
)


def _scan_cli_for_legacy_commands() -> list[str]:
    """Check cli.py doesn't import write commands from legacy commands module."""
    violations = []
    cli_path = SRC_ROOT / "watercooler" / "cli.py"

    if not cli_path.exists():
        return violations

    content = cli_path.read_text(encoding="utf-8")
    for line_no, line in enumerate(content.splitlines(), 1):
        m = _LEGACY_CMD_RE.match(line)
        if not m:
            continue
        imported = {name.strip() for name in m.group(1).split(",")}
        for func in imported & _GRAPH_WRITE_COMMANDS:
            violations.append(
                f"cli.py:{line_no}: imports '{func}' from .commands "
                f"instead of .commands_graph"
            )

    return violations


class TestGraphFirstInvariant:
    """Ensure deprecated .md-parsing functions are not used in runtime code."""

    def test_no_deprecated_md_imports_in_runtime(self):
        """Runtime src/ code must not import discover_thread_files,
        parse_thread_entries, or parse_thread_header.

        These are deprecated in favor of graph reader functions:
        - storage.list_thread_topics()
        - writer.get_thread_from_graph()
        - writer.get_entries_for_thread()
        - reader.format_thread_markdown()
        """
        violations = _scan_for_deprecated_imports()

        if violations:
            msg = (
                "Deprecated .md-parsing imports found in runtime code.\n"
                "Graph is the sole source of truth — use graph reader "
                "functions instead.\n\n"
                + "\n".join(f"  {v}" for v in violations)
            )
            pytest.fail(msg)

    def test_cli_uses_graph_canonical_commands(self):
        """CLI must import write commands from commands_graph, not commands.

        The legacy commands module reads/writes .md directly. CLI must use
        the graph-canonical versions that write graph first, then project.
        """
        violations = _scan_cli_for_legacy_commands()

        if violations:
            msg = (
                "CLI imports write commands from legacy .commands module.\n"
                "Use .commands_graph instead (graph-first, then project).\n\n"
                + "\n".join(f"  {v}" for v in violations)
            )
            pytest.fail(msg)
