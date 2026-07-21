"""Blessed-surface projection — promoted lessons collect in `team-lessons`.

Phase 3 of the Commons cluster (Decision 01KXQ32Q7Z41F0P7A1JHN0S527, D2 option
(c); plan workflow-packs-prepare-work-discovery-2026-05-29:85). On a successful
Learning promotion, a compact pointer Note is projected to the blessed thread
with provenance, plus xref annotations both ways. The projection is
**independently retryable and idempotent per leg** (review P1-3): promotion
must never be re-run to repair it (`validate_candidate_for_promotion`'s
`Promoted-From` guard correctly refuses that) — instead
:func:`reconcile_blessed_projection` detects and completes each leg:

1. the pointer Note on the blessed thread (dedup key: ``Blessed-Lesson:``),
2. an ``xref`` on the source lesson entry → the pointer,
3. an ``xref`` on the pointer entry → the source lesson.

Writers are injected so each caller supplies its own sync discipline: the MCP
promote path uses its ack/annotate impls (per-call sync), the CLI wraps lib
writers in per-topic ``_cli_write_with_sync`` calls. Detection logic lives here
exactly once.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

DEFAULT_BLESSED_THREAD = "team-lessons"

# Pointer dedup key. One pointer per promoted lesson; a re-run that finds this
# marker on the blessed thread treats leg 1 as present.
_BLESSED_LESSON_RE = re.compile(
    r"^Blessed-Lesson:\s*([0-9A-HJKMNP-TV-Z]{26})\s*$", re.MULTILINE
)

# PointerWriter(title, body) -> entry_id of the written pointer Note (or None
# on failure). XrefWriter(topic, target_entry_id, value_entry_id) -> bool.
PointerWriter = Callable[[str, str], Optional[str]]
XrefWriter = Callable[[str, str, str], bool]
# Read adapters (review #1131 P1: hosted mode has no local baseline filesystem,
# so inspection accepts injected readers the same way the write legs accept
# injected writers). EntriesLoader(topic) -> entry dicts. XrefsLoader(topic,
# target_entry_id) -> the target's current xref values (bare ULIDs).
EntriesLoader = Callable[[str], list]
XrefsLoader = Callable[[str, str], list]


def _local_entries_loader(threads_dir: Path) -> EntriesLoader:
    def _load(topic: str) -> list:
        from .baseline_graph import storage
        from .baseline_graph.storage import get_graph_dir

        try:
            return list(storage.load_thread_entries(get_graph_dir(threads_dir), topic))
        except Exception:
            return []

    return _load


def _local_xrefs_loader(threads_dir: Path) -> XrefsLoader:
    def _load(topic: str, target_entry_id: str) -> list:
        from .baseline_graph.annotations import get_annotation_state
        from .baseline_graph.storage import get_graph_dir, get_thread_graph_dir

        state = get_annotation_state(
            get_thread_graph_dir(get_graph_dir(threads_dir), topic),
            target_entry_id,
            read_only=True,
        )
        return list(state.xrefs)

    return _load


def _bare_id(value: Any) -> str:
    """Bare ULID from a graph node id (entries.jsonl stores ``entry:<ULID>``).

    Annotation state and xref values carry the bare form — comparisons must
    normalize or lookups silently fail (same convention as the decisions
    tool's ``_bare_entry_id``).
    """
    text = str(value or "")
    return text.split(":", 1)[1] if text.startswith("entry:") else text


@dataclass
class ProjectionLegs:
    """Observed state of the three projection legs (read-only inspection)."""

    lesson_found: bool = False
    lesson_title: str = ""
    lesson_summary: str = ""
    candidate_entry_id: Optional[str] = None
    root_cause_canonical: Optional[str] = None
    source_index: Optional[int] = None
    pointer_entry_id: Optional[str] = None
    xref_lesson_present: bool = False
    xref_pointer_present: bool = False


@dataclass
class ReconcileResult:
    """Per-leg outcome: ``present`` | ``created`` | ``failed`` | ``skipped``."""

    pointer: str = "skipped"
    xref_lesson: str = "skipped"
    xref_pointer: str = "skipped"
    pointer_entry_id: Optional[str] = None
    errors: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return all(
            leg in ("present", "created")
            for leg in (self.pointer, self.xref_lesson, self.xref_pointer)
        )


def format_blessed_pointer_body(
    *,
    lesson_entry_id: str,
    source_topic: str,
    source_index: Optional[int],
    candidate_entry_id: Optional[str],
    lesson_summary: str,
    root_cause_canonical: Optional[str],
) -> str:
    """Body of the pointer Note projected to the blessed thread.

    The durable lesson stays on its source thread — this is the index entry
    with provenance, not a second copy. Markers keep the pointer machine-
    detectable (``Blessed-Lesson:`` is the reconcile dedup key) without
    matching any promotion-guard pattern (its Spec does not end in
    ``-promoted``, and it carries no ``Authority-Basis`` line).
    """
    src_ref = (
        f"{source_topic}:{source_index} ({lesson_entry_id})"
        if source_index is not None
        else f"{source_topic} ({lesson_entry_id})"
    )
    lines = [
        "Spec: team-lessons-pointer",
        f"Blessed-Lesson: {lesson_entry_id}",
        f"Blessed-Source-Topic: {source_topic}",
    ]
    if candidate_entry_id:
        lines.append(f"Promoted-From-Candidate: {candidate_entry_id}")
    if root_cause_canonical:
        lines.append(f"Root-Cause-Canonical: {root_cause_canonical}")
    lines += [
        "",
        "## Blessed lesson",
        lesson_summary.strip() or "(no summary captured)",
        "",
        "## Provenance",
        f"Durable lesson: `{src_ref}` (a human-promoted `## Lesson` Note; this "
        "pointer is the blessed-surface index entry, written by the promotion "
        "path and repairable via `watercooler reconcile-blessed-projection`).",
    ]
    return "\n".join(lines)


def find_blessed_pointer(
    blessed_entries: list[dict[str, Any]], lesson_entry_id: str
) -> Optional[str]:
    """Entry ID of the existing pointer Note for *lesson_entry_id*, if any."""
    for entry in blessed_entries:
        body = entry.get("body")
        if not isinstance(body, str):
            continue
        m = _BLESSED_LESSON_RE.search(body)
        if m and m.group(1) == lesson_entry_id:
            return _bare_id(entry.get("entry_id") or entry.get("id") or "") or None
    return None


def _first_lesson_paragraph(body: str) -> str:
    m = re.search(
        r"^##\s+Lesson\s*\n+([^\n#][^\n]*(?:\n[^#\n][^\n]*)*)", body, re.MULTILINE
    )
    return m.group(1).strip() if m else ""


def inspect_blessed_projection(
    threads_dir: Path,
    source_topic: str,
    lesson_entry_id: str,
    *,
    blessed_topic: str = DEFAULT_BLESSED_THREAD,
    entries_loader: Optional[EntriesLoader] = None,
    xrefs_loader: Optional[XrefsLoader] = None,
) -> ProjectionLegs:
    """Read-only observation of all three legs (tolerates every partial state).

    ``entries_loader`` / ``xrefs_loader`` inject the read surface: local
    baseline-graph readers by default; hosted callers supply GitHub-backed
    readers (review #1131 P1 — hosted mode has no local filesystem to read).
    """
    from .promotion import _extract_promoted_from, parse_candidate_body

    load_entries = entries_loader or _local_entries_loader(threads_dir)
    load_xrefs = xrefs_loader or _local_xrefs_loader(threads_dir)

    legs = ProjectionLegs()
    source_entries = list(load_entries(source_topic))
    lesson = None
    for entry in source_entries:
        if _bare_id(entry.get("id") or entry.get("entry_id") or "") == lesson_entry_id:
            lesson = entry
            break
    if lesson is None:
        return legs
    body = lesson.get("body") or ""
    candidate = _extract_promoted_from(lesson)
    if candidate is None:
        # Not a genuine promoted lesson — nothing to project.
        return legs
    legs.lesson_found = True
    legs.lesson_title = str(lesson.get("title") or "")
    legs.lesson_summary = _first_lesson_paragraph(body)
    legs.candidate_entry_id = candidate
    meta = parse_candidate_body(body, lesson_entry_id, source_topic)
    if meta.root_cause_canonical and meta.root_cause_taxonomy_version is not None:
        legs.root_cause_canonical = (
            f"{meta.root_cause_canonical}@{meta.root_cause_taxonomy_version}"
        )
    # Provenance ref detail: the lesson's own index, when the node carries it.
    legs_index = lesson.get("index")
    legs.source_index = legs_index if isinstance(legs_index, int) else None

    blessed_entries = list(load_entries(blessed_topic))
    legs.pointer_entry_id = find_blessed_pointer(blessed_entries, lesson_entry_id)

    if legs.pointer_entry_id:
        legs.xref_lesson_present = legs.pointer_entry_id in load_xrefs(
            source_topic, lesson_entry_id
        )
        legs.xref_pointer_present = lesson_entry_id in load_xrefs(
            blessed_topic, legs.pointer_entry_id
        )
    return legs


def _default_pointer_writer(
    threads_dir: Path, blessed_topic: str, actor: str
) -> PointerWriter:
    def _write(title: str, body: str) -> Optional[str]:
        from ulid import ULID

        from .commands_graph import ack

        entry_id = str(ULID())
        # Ball-preserving by construction — a projection must not grab the
        # blessed thread's attention state.
        ack(
            blessed_topic,
            threads_dir=threads_dir,
            agent=actor,
            role="scribe",
            title=title,
            entry_type="Note",
            body=body,
            entry_id=entry_id,
        )
        return entry_id

    return _write


def _default_xref_writer(threads_dir: Path, actor: str) -> XrefWriter:
    def _write(topic: str, target_entry_id: str, value_entry_id: str) -> bool:
        from ulid import ULID

        from .baseline_graph.annotations import AnnotationEvent, append_annotation
        from .baseline_graph.storage import get_graph_dir, get_thread_graph_dir

        append_annotation(
            get_thread_graph_dir(get_graph_dir(threads_dir), topic),
            AnnotationEvent(
                id=str(ULID()),
                target_id=target_entry_id,
                target_type="entry",
                kind="xref",
                value=value_entry_id,
                actor=actor,
                timestamp=datetime.now(timezone.utc).isoformat(),
            ),
        )
        return True

    return _write


def reconcile_blessed_projection(
    threads_dir: Path,
    source_topic: str,
    lesson_entry_id: str,
    *,
    blessed_topic: str = DEFAULT_BLESSED_THREAD,
    actor: str = "Blessed Projection",
    pointer_writer: Optional[PointerWriter] = None,
    xref_writer: Optional[XrefWriter] = None,
    entries_loader: Optional[EntriesLoader] = None,
    xrefs_loader: Optional[XrefsLoader] = None,
) -> ReconcileResult:
    """Detect and complete the blessed projection for one promoted lesson.

    Idempotent per leg: a present leg is left untouched; a missing leg is
    written; a failing leg is recorded (``failed``) without blocking the
    others. Safe from every partial-write ordering — including a pointer that
    exists with neither xref, or a single stray xref.

    Args:
        pointer_writer / xref_writer: Sync-discipline injection (MCP impls or
            CLI-wrapped lib writers). Defaults write through the lib's
            ball-preserving ``ack`` + ``append_annotation`` with NO git sync —
            callers owning a git-backed worktree must wrap or supply writers.
        entries_loader / xrefs_loader: Read-surface injection (hosted callers
            pass GitHub-backed readers; defaults read the local baseline graph).
    """
    result = ReconcileResult()
    legs = inspect_blessed_projection(
        threads_dir, source_topic, lesson_entry_id, blessed_topic=blessed_topic,
        entries_loader=entries_loader, xrefs_loader=xrefs_loader,
    )
    if not legs.lesson_found:
        result.errors.append(
            f"lesson {lesson_entry_id} not found on '{source_topic}' or not a "
            f"genuine promoted lesson (Promoted-From + authority markers required)"
        )
        result.pointer = result.xref_lesson = result.xref_pointer = "failed"
        return result

    p_writer = pointer_writer or _default_pointer_writer(
        threads_dir, blessed_topic, actor
    )
    x_writer = xref_writer or _default_xref_writer(threads_dir, actor)

    # Leg 1 — pointer Note.
    if legs.pointer_entry_id:
        result.pointer = "present"
        result.pointer_entry_id = legs.pointer_entry_id
    else:
        try:
            title = legs.lesson_title or f"Blessed lesson {lesson_entry_id}"
            body = format_blessed_pointer_body(
                lesson_entry_id=lesson_entry_id,
                source_topic=source_topic,
                source_index=legs.source_index,
                candidate_entry_id=legs.candidate_entry_id,
                lesson_summary=legs.lesson_summary,
                root_cause_canonical=legs.root_cause_canonical,
            )
            new_id = p_writer(title, body)
            if new_id:
                result.pointer = "created"
                result.pointer_entry_id = new_id
            else:
                result.pointer = "failed"
                result.errors.append("pointer write returned no entry id")
        except Exception as exc:  # noqa: BLE001 — legs stay independent
            result.pointer = "failed"
            result.errors.append(f"pointer write failed: {exc}")

    pointer_id = result.pointer_entry_id
    if pointer_id is None:
        # Without a pointer the xrefs have no counterpart — report and stop.
        result.xref_lesson = result.xref_pointer = "failed"
        return result

    # Leg 2 — xref on the source lesson entry → pointer.
    if legs.xref_lesson_present:
        result.xref_lesson = "present"
    else:
        try:
            result.xref_lesson = (
                "created" if x_writer(source_topic, lesson_entry_id, pointer_id)
                else "failed"
            )
        except Exception as exc:  # noqa: BLE001
            result.xref_lesson = "failed"
            result.errors.append(f"source xref failed: {exc}")

    # Leg 3 — xref on the pointer entry → source lesson.
    if legs.xref_pointer_present:
        result.xref_pointer = "present"
    else:
        try:
            result.xref_pointer = (
                "created" if x_writer(blessed_topic, pointer_id, lesson_entry_id)
                else "failed"
            )
        except Exception as exc:  # noqa: BLE001
            result.xref_pointer = "failed"
            result.errors.append(f"pointer xref failed: {exc}")

    return result
