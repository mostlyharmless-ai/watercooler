"""Daemon no-auto-promote guardrail (#epistemic-custody-v1).

The only daemon call site of daemon_write_entry() that may write authority entries
must live in decision_extractor.py via the ExtractDecisionsDaemon 8-gate path.
Every other daemon stays a Note / advisory / status-report producer.

An "authority write" is any daemon_write_entry() call that:
- passes entry_type="Decision" or entry_type="Closure", OR
- passes a non-None, non-empty authority_fields keyword argument.

Scope: the scanner resolves only literal AST constants. Variable-indirected
entry_type (e.g. entry_type=ETYPE where ETYPE="Decision") is not detected — that
case is a known gap, accepted for a ratification-pinning PR. A code-review gate is
the backstop for variable-indirection patterns.

If you are writing a new daemon that legitimately needs to write Decisions or Closures,
document that need and update _AUTHORIZED_DECISION_MODULE — do not weaken the test.
"""

import ast
from pathlib import Path


_DAEMONS_DIR = (
    Path(__file__).parent.parent.parent
    / "src" / "watercooler_mcp" / "daemons"
)

# Only this module may pass authority-write arguments to daemon_write_entry().
_AUTHORIZED_DECISION_MODULE = "decision_extractor.py"

_AUTHORITY_ENTRY_TYPES = {"Decision", "Closure"}


def _is_authority_write_call(node: ast.Call) -> bool:
    """Return True if this daemon_write_entry call is an authority write.

    Detects:
    - entry_type="Decision" or entry_type="Closure" (literal constant only)
    - authority_fields= with any non-None, non-empty value
      (None and {} are the inert defaults; any richer value signals authority intent)
    """
    for kw in node.keywords:
        if kw.arg == "entry_type" and isinstance(kw.value, ast.Constant):
            if kw.value.value in _AUTHORITY_ENTRY_TYPES:
                return True
        if kw.arg == "authority_fields":
            val = kw.value
            # None → inert default, not an authority write.
            if isinstance(val, ast.Constant) and val.value is None:
                continue
            # {} → empty dict, also inert; callers should use None but {} is equivalent.
            if isinstance(val, ast.Dict) and len(val.keys) == 0:
                continue
            return True
    return False


def _is_daemon_write_entry_call(node: ast.Call) -> bool:
    func = node.func
    return (
        (isinstance(func, ast.Name) and func.id == "daemon_write_entry")
        or (isinstance(func, ast.Attribute) and func.attr == "daemon_write_entry")
    )


def _find_authority_write_calls(src: str) -> list[int]:
    """Return line numbers of daemon_write_entry authority-write calls.

    Raises SyntaxError if src is not valid Python — a parse failure in a daemon
    file is itself a bug and should surface as a red test, not a silent skip.
    """
    tree = ast.parse(src)
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _is_daemon_write_entry_call(node)
        and _is_authority_write_call(node)
    ]


def _find_decision_write_calls(src: str) -> list[int]:
    """Return line numbers of daemon_write_entry calls with entry_type='Decision'."""
    tree = ast.parse(src)
    hits = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_daemon_write_entry_call(node)):
            continue
        for kw in node.keywords:
            if (
                kw.arg == "entry_type"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value == "Decision"
            ):
                hits.append(node.lineno)
    return hits


class TestDaemonNoAutoPromote:
    """Only ExtractDecisionsDaemon may write Decision/Closure or pass authority_fields."""

    def test_only_decision_extractor_writes_authority_entries(self):
        violations: list[str] = []

        for py_file in _DAEMONS_DIR.rglob("*.py"):
            if py_file.name == _AUTHORIZED_DECISION_MODULE:
                continue
            src = py_file.read_text(encoding="utf-8")
            for line in _find_authority_write_calls(src):
                violations.append(
                    f"{py_file.relative_to(_DAEMONS_DIR.parent.parent.parent)}:{line}"
                )

        assert violations == [], (
            "Unauthorized daemon authority writer(s) detected:\n"
            + "\n".join(f"  {v}" for v in violations)
            + "\n\nOnly decision_extractor.py (ExtractDecisionsDaemon 8-gate path) may "
            "pass entry_type='Decision'/'Closure' or authority_fields to "
            "daemon_write_entry(). See epistemic-custody-v1 ratification."
        )

    def test_decision_extractor_still_writes_decision_entries(self):
        # Sanity-check: the authorized module must still have at least one call with
        # entry_type="Decision" specifically — not just authority_fields on a Note path.
        # If this fails, the Decision-writing path moved and _AUTHORIZED_DECISION_MODULE
        # needs updating.
        authorized = _DAEMONS_DIR / _AUTHORIZED_DECISION_MODULE
        src = authorized.read_text(encoding="utf-8")
        hits = _find_decision_write_calls(src)
        assert hits, (
            "decision_extractor.py no longer calls daemon_write_entry with "
            "entry_type='Decision'. If the decision-writing path moved, update "
            "_AUTHORIZED_DECISION_MODULE in this test."
        )
