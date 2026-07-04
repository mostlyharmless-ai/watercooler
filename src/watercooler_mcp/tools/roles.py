"""Role discovery tool for the watercooler MCP server.

Tool:
- watercooler_roles: the project's role catalog, or — when ``role`` is given
  — the full behavioral spec for that single role. With
  ``action="ledger"``, returns the Role Salience Compiler's projection
  ledger audit (ledgered/unledgered bullets + retirement-due bullets) for
  a role. With ``action="compile"`` and ``bullets=[...]``, returns a dry-run
  preview of compiling those candidate bullets into the role's salience
  (accepted / needs_rewrite / dropped-with-reason) without writing anything
  — the L1/L2 preview a human runs via the ``update-roles-context`` skill,
  made reachable without local Python execution.

PR3b consolidation: ``watercooler_role_details`` was folded into
``watercooler_roles(role=...)``; the old name forwards via a deprecation
alias (see ``watercooler_mcp/aliases.py``).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from watercooler.role_loader import load_roles


# Module-level reference to the registered tool (populated by register_role_tools)
roles = None

_LEDGER_PATH = Path.home() / ".watercooler" / "role_salience_ledger.jsonl"


def _ledger_impl(code_path: str, role: str) -> str:
    """Return the Role Salience Compiler's ledger audit for one role.

    Wraps ``role_salience_lib.verify_ledger_provenance`` and
    ``find_review_due_bullets`` so an MCP client without local
    filesystem/Python-execution access (e.g. a hosted or non-Bash-capable
    client) can run the ``update-roles-context`` skill's Step 1.5/1.6
    audits via a tool call instead of inline Python.
    """
    from watercooler.role_salience_lib import (
        find_review_due_bullets,
        verify_ledger_provenance,
    )

    if not role:
        return json.dumps({"error": "role_required_for_ledger_action"})

    try:
        loaded = load_roles(code_path or None)
    except Exception as exc:
        return json.dumps({"error": "load_failed", "detail": str(exc)})

    normalized = role.strip().lower()
    if normalized not in loaded:
        return json.dumps({
            "error": "unknown_role",
            "role": role,
            "valid_roles": sorted(loaded.keys()),
        })

    definition = loaded[normalized]
    provenance = verify_ledger_provenance(definition, _LEDGER_PATH)
    review_due = find_review_due_bullets(definition, _LEDGER_PATH)

    return json.dumps(
        {
            "role": normalized,
            "ledgered": list(provenance.ledgered),
            "unledgered": list(provenance.unledgered),
            "review_due": [
                {
                    "text": b.text,
                    "reason": b.reason,
                    "review_after": b.review_after,
                }
                for b in review_due
            ],
        },
        indent=2,
    )


def _preview_impl(code_path: str, role: str, bullets: "list[str] | None") -> str:
    """Dry-run compile of candidate salience bullets for one role — no write.

    Wraps ``role_salience_lib.compile_project_salience`` so an MCP client
    without local Python execution can preview the L1/L2 compile artifact
    (which candidate bullets are accepted, which need rewrite, which are
    dropped and why) before a human lands the patch. Nothing is written to
    disk — landing the patch remains an explicit human (L3) step.
    """
    from watercooler.role_salience_lib import (
        PromotedLessonBullet,
        compile_project_salience,
    )

    if not role:
        return json.dumps({"error": "role_required_for_compile_action"})

    try:
        loaded = load_roles(code_path or None)
    except Exception as exc:
        return json.dumps({"error": "load_failed", "detail": str(exc)})

    normalized = role.strip().lower()
    if normalized not in loaded:
        return json.dumps({
            "error": "unknown_role",
            "role": role,
            "valid_roles": sorted(loaded.keys()),
        })

    definition = loaded[normalized]
    candidates = []
    for text in bullets or []:
        if not isinstance(text, str):
            return json.dumps({
                "error": "invalid_bullet",
                "detail": f"every bullet must be a string, got {type(text).__name__}",
            })
        candidates.append(
            PromotedLessonBullet(
                role=definition.name, text=text, source_lesson_ulid="preview"
            )
        )

    patch = compile_project_salience(
        promoted_lessons=candidates, current_definition=definition
    )
    return json.dumps(
        {
            "role": patch.role,
            "has_changes": patch.has_changes,
            "accepted": [b.text for b in patch.accepted],
            "needs_rewrite": [b.text for b in patch.needs_rewrite],
            "dropped": [{"text": b.text, "reason": b.reason} for b in patch.dropped],
        },
        indent=2,
    )


def _roles_impl(
    code_path: str = "",
    role: str = "",
    action: str = "",
    bullets: "list[str] | None" = None,
) -> str:
    """Return the project's role catalog, or one role's full behavioral spec.

    With no ``role``, returns the compact catalog — name, description,
    canonical_role, produces, boundary, when_to_use, handoff_to for every
    role. With a ``role`` name, returns that role's full spec: the compact
    fields plus instructions, entry_style, and collaborate_with.

    Args:
        code_path: Path to the project repository root. Uses bundled
            defaults when empty.
        role: Optional role name (e.g. "critic", "implementer"). Empty
            returns the full catalog.
        action: Optional. ``"ledger"`` returns the Role Salience Compiler's
            projection-ledger audit for ``role`` (requires ``role``);
            ``"compile"`` returns a dry-run preview of compiling ``bullets``
            into ``role``'s salience (requires ``role``) — no disk write.
        bullets: Candidate bullet texts for ``action="compile"``. Ignored
            for other actions.

    Returns:
        JSON — the catalog object, the single-role spec, a ledger audit
        (``action="ledger"``), a compile preview (``action="compile"``), or
        an error with a ``valid_roles`` list.
    """
    if action == "ledger":
        return _ledger_impl(code_path, role)
    if action == "compile":
        return _preview_impl(code_path, role, bullets)

    try:
        loaded = load_roles(code_path or None)
    except Exception as exc:
        return json.dumps({"error": "load_failed", "detail": str(exc)})

    if role:
        normalized = role.strip().lower()
        if normalized not in loaded:
            return json.dumps({
                "error": "unknown_role",
                "role": role,
                "valid_roles": sorted(loaded.keys()),
            })
        return json.dumps(asdict(loaded[normalized]), indent=2)

    result = {}
    for name, rd in loaded.items():
        result[name] = {
            "description": rd.description,
            "canonical_role": rd.canonical_role,
            "produces": rd.produces,
            "boundary": rd.boundary,
            "when_to_use": rd.when_to_use,
            "handoff_to": rd.handoff_to,
            "project_salience": rd.project_salience,
        }

    return json.dumps(result, indent=2)


def register_role_tools(mcp):
    """Register the role discovery tool with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """
    global roles

    roles = mcp.tool(name="watercooler_roles")(_roles_impl)
