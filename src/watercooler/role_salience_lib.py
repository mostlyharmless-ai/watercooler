"""Pure compiler for the Role Salience Compiler.

Turns human-promoted Lesson bullets into a validated, capped
``project_salience`` patch for one role, then (optionally) writes that patch
into a project's ``.watercooler/roles.toml`` via ``tomlkit``, preserving
comments and every other field.

Stdlib-only except for the optional, lazily-imported ``tomlkit`` writer
functions — the compile step (``compile_project_salience``) has zero
dependencies beyond ``watercooler.role_loader`` and
``watercooler.pulse_stance_lib`` (both themselves stdlib-only).

See ``dev_docs/plans/2026-06-30-feat-role-salience-compiler-plan.md``
(Phase 2) and
``dev_docs/brainstorms/2026-06-30-feat-role-salience-compiler-brainstorm.md``
(D8: attention-vs-policy lint) for the design this module implements.

This module never queries threads, never decides what to promote, and never
writes to disk on its own — the ``update-roles-context`` skill (an agent,
with mandatory human L2 review) is the only caller, and it is responsible
for sourcing eligible promoted-Lesson bullets, presenting the diff, and
getting explicit confirmation before any write lands.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Literal, Optional

from watercooler.pulse_stance_lib import normalize_salience_bullet
from watercooler.role_loader import RoleDefinition

# Caps/ordering (D8 + Phase 2 acceptance). default_cap is the normal ceiling;
# SALIENCE_HARD_CAP is the absolute maximum compile_project_salience accepts
# as an override — callers must not silently exceed it.
SALIENCE_DEFAULT_CAP = 5
SALIENCE_HARD_CAP = 7
BULLET_MAX_CHARS = 160

# Attention-vs-policy lint (D8), two tiers.
#   hard_block: explicit authority/policy vocabulary — never written, even
#   after a human rewrite, because it asserts policy authority the daemon
#   findings channel does not have.
#   needs_rewrite: directive vocabulary that reads as a command rather than
#   an attention cue (e.g. "critic must reject X"). The skill's L2 human
#   review rewrites it to an attention/question shape ("critic: notice X
#   risk") or drops it — this library never auto-rewrites.
_HARD_BLOCK_RE = re.compile(
    r"\b(authoriz(?:ed|es|ation)|canonical|policy|approv(?:e|ed|es|al)"
    r"|forbid(?:s|den)?|enforc(?:e|ed|es|ing|ement)|ban(?:s|ned)?)\b",
    re.IGNORECASE,
)
_NEEDS_REWRITE_RE = re.compile(
    r"\b(must|never|requir(?:e|es|ed)|should not|do not|block(?:s|ed)?"
    r"|reject(?:s|ed)?|allow(?:s|ed)?|disallow(?:s|ed)?|den(?:y|ies|ied))\b",
    re.IGNORECASE,
)

LintStatus = Literal["ok", "needs_rewrite", "hard_block"]
WriteMode = Literal["complete_override", "partial_override", "bundled_only"]
LedgerStatus = Literal["active", "retired", "superseded"]
_VALID_LEDGER_STATUSES = frozenset({"active", "retired", "superseded"})

# Standard fields a "complete" project-override block carries — every
# RoleDefinition field except `name` (implicit from the TOML table key) and
# `project_salience` (the field this compiler writes).
_COMPLETE_OVERRIDE_FIELDS = frozenset(
    {
        "description",
        "canonical_role",
        "produces",
        "boundary",
        "handoff_to",
        "instructions",
        "entry_style",
        "when_to_use",
        "collaborate_with",
    }
)


@dataclass(frozen=True)
class PromotedLessonBullet:
    """One candidate ``project_salience`` bullet drafted from a promoted Lesson.

    Turning a promoted Lesson's text into a short attention bullet is a
    semantic step done by the ``update-roles-context`` skill (an agent, with
    L2 human review) *before* calling this library — this dataclass is the
    library's input contract, not something it derives on its own.
    """

    role: str
    text: str
    source_lesson_ulid: str
    confidence: Optional[float] = None
    review_after: Optional[str] = None


@dataclass(frozen=True)
class SalienceBullet:
    """An accepted, normalized ``project_salience`` bullet ready to write."""

    text: str
    source_lesson_ulid: str
    confidence: Optional[float] = None
    review_after: Optional[str] = None


@dataclass(frozen=True)
class DroppedBullet:
    """A bullet excluded from the patch, with the reason for the audit trail.

    No silent truncation: every drop (lint, cap, length, duplicate) is
    recorded here so the skill can show the human what was excluded and why.
    """

    text: str
    source_lesson_ulid: str
    reason: str


@dataclass(frozen=True)
class SaliencePatch:
    """Compiled result for one role: what to write, flag, and drop.

    ``has_changes`` is set explicitly by the producing function (NOT
    derived from ``bool(accepted)``) — a role that already has bullets
    from a prior compile has a non-empty ``accepted`` list even when
    nothing about it changed this cycle, so ``bool(accepted)`` would
    false-positive on the common "already compiled once, nothing new
    this run" case and needlessly rewrite the TOML block.
    """

    role: str
    accepted: tuple[SalienceBullet, ...]
    needs_rewrite: tuple[PromotedLessonBullet, ...]
    dropped: tuple[DroppedBullet, ...]
    has_changes: bool


def lint_bullet(text: str) -> LintStatus:
    """Classify a bullet per the D8 attention-vs-policy lint."""
    if _HARD_BLOCK_RE.search(text):
        return "hard_block"
    if _NEEDS_REWRITE_RE.search(text):
        return "needs_rewrite"
    return "ok"


def compile_project_salience(
    *,
    promoted_lessons: list[PromotedLessonBullet],
    current_definition: RoleDefinition,
    default_cap: int = SALIENCE_DEFAULT_CAP,
) -> SaliencePatch:
    """Compile promoted-Lesson bullets into a capped, linted patch for one role.

    Existing ``project_salience`` bullets on ``current_definition`` are
    preserved and ordered first (their provenance predates this compile run
    and is not re-derivable here); new promoted bullets are appended in
    input order. The combined list is capped at ``default_cap``, with
    anything beyond the cap moved to ``dropped`` rather than silently lost.

    Args:
        promoted_lessons: Candidate bullets for THIS role only — the caller
            (the ``update-roles-context`` skill) filters by role and by the
            promoted-Lesson eligibility predicate before calling this.
        current_definition: The role's current MERGED definition (bundled
            defaults + any existing project override); only its
            ``project_salience`` is read, for preservation.
        default_cap: Soft cap on total bullets (existing + new). Must not
            exceed ``SALIENCE_HARD_CAP``.

    Returns:
        SaliencePatch with accepted/needs_rewrite/dropped bullets.

    Raises:
        ValueError: If ``default_cap`` exceeds ``SALIENCE_HARD_CAP``, or any
            ``promoted_lessons`` entry's ``role`` does not match
            ``current_definition.name``.
    """
    if default_cap > SALIENCE_HARD_CAP:
        raise ValueError(
            f"default_cap={default_cap} exceeds SALIENCE_HARD_CAP={SALIENCE_HARD_CAP}"
        )

    role = current_definition.name
    existing_texts = current_definition.project_salience
    existing = [
        SalienceBullet(text=t, source_lesson_ulid="") for t in existing_texts
    ]
    accepted: list[SalienceBullet] = list(existing)
    needs_rewrite: list[PromotedLessonBullet] = []
    dropped: list[DroppedBullet] = []
    seen_normalized = {normalize_salience_bullet(b.text) for b in existing}

    for candidate in promoted_lessons:
        if candidate.role != role:
            raise ValueError(
                f"PromotedLessonBullet.role={candidate.role!r} does not match "
                f"current_definition.name={role!r}"
            )
        text = candidate.text.strip()
        if not text:
            dropped.append(
                DroppedBullet(
                    text=candidate.text,
                    source_lesson_ulid=candidate.source_lesson_ulid,
                    reason="empty",
                )
            )
            continue
        if len(text) > BULLET_MAX_CHARS:
            dropped.append(
                DroppedBullet(
                    text=text,
                    source_lesson_ulid=candidate.source_lesson_ulid,
                    reason=f"too_long(>{BULLET_MAX_CHARS} chars)",
                )
            )
            continue

        status = lint_bullet(text)
        if status == "hard_block":
            dropped.append(
                DroppedBullet(
                    text=text,
                    source_lesson_ulid=candidate.source_lesson_ulid,
                    reason="hard_block: policy/authority vocabulary",
                )
            )
            continue
        if status == "needs_rewrite":
            needs_rewrite.append(candidate)
            continue

        normalized = normalize_salience_bullet(text)
        if normalized in seen_normalized:
            dropped.append(
                DroppedBullet(
                    text=text,
                    source_lesson_ulid=candidate.source_lesson_ulid,
                    reason="duplicate",
                )
            )
            continue
        seen_normalized.add(normalized)
        accepted.append(
            SalienceBullet(
                text=text,
                source_lesson_ulid=candidate.source_lesson_ulid,
                confidence=candidate.confidence,
                review_after=candidate.review_after,
            )
        )

    # The cap applies ONLY to newly-added bullets. Existing committed bullets
    # are never dropped by the cap: a role may legitimately carry up to
    # SALIENCE_HARD_CAP hand-authored bullets, and trimming them would
    # silently propose deleting the human's own content on a run with nothing
    # new (flipping has_changes on a no-op). New additions that do not fit
    # under default_cap overflow to `dropped` (cap_exceeded) — the deferred
    # new-bullet FIFO behavior. When existing already fills or exceeds the
    # cap, all existing are kept and no new bullet is added.
    n_existing = len(existing)
    if len(accepted) > default_cap:
        keep_new = max(0, default_cap - n_existing)
        new_bullets = accepted[n_existing:]
        kept_new, overflow = new_bullets[:keep_new], new_bullets[keep_new:]
        accepted = list(existing) + kept_new
        dropped.extend(
            DroppedBullet(
                text=b.text,
                source_lesson_ulid=b.source_lesson_ulid,
                reason=f"cap_exceeded(default_cap={default_cap})",
            )
            for b in overflow
        )

    # changed iff the final accepted list differs (content or order) from
    # the role's pre-existing project_salience — NOT just "accepted is
    # non-empty", which would false-positive whenever a role already had
    # bullets and nothing new was compiled this cycle.
    existing_normalized_ordered = [normalize_salience_bullet(t) for t in existing_texts]
    accepted_normalized_ordered = [normalize_salience_bullet(b.text) for b in accepted]
    changed = existing_normalized_ordered != accepted_normalized_ordered

    return SaliencePatch(
        role=role,
        accepted=tuple(accepted),
        needs_rewrite=tuple(needs_rewrite),
        dropped=tuple(dropped),
        has_changes=changed,
    )


def classify_write_mode(role_name: str, project_toml_doc) -> WriteMode:
    """Classify how to splice a salience patch into a project's roles.toml.

    Mirrors the loader's whole-block-replace semantics
    (``role_loader.py::load_roles``, ``merged.update(overrides)``): a
    project block that omits a field gets the dataclass *default* at load
    time, not the bundled value — so writing ``project_salience`` into an
    existing-but-partial block by adding just that one key would silently
    empty out the role's other fields on next load. Returns:

    - ``"complete_override"``: the block exists and already carries every
      standard field — safe to update ``project_salience`` in place.
    - ``"partial_override"``: the block exists but omits one or more
      standard fields — must regenerate the complete block.
    - ``"bundled_only"``: the role has no project block at all yet — must
      generate the complete block from the bundled catalog.

    Args:
        role_name: The role to classify.
        project_toml_doc: A ``tomlkit`` document parsed from the project's
            ``.watercooler/roles.toml`` (or an empty ``tomlkit.document()``
            if the file does not exist yet).
    """
    roles_table = project_toml_doc.get("roles")
    if roles_table is None or role_name not in roles_table:
        return "bundled_only"
    block = roles_table[role_name]
    present = set(block.keys()) if hasattr(block, "keys") else set()
    if _COMPLETE_OVERRIDE_FIELDS.issubset(present):
        return "complete_override"
    return "partial_override"


def apply_salience_patch(
    *,
    project_toml_doc,
    patch: SaliencePatch,
    bundled_definition: RoleDefinition,
) -> None:
    """Apply ``patch`` to ``project_toml_doc`` in place.

    A no-op when ``patch.has_changes`` is False (the "nothing to compile"
    clean path — Phase 2 acceptance criterion). The caller is responsible
    for parsing the document, calling this, rendering with
    ``tomlkit.dumps()``, showing a full-file diff, and getting explicit
    human confirmation before writing to disk — this function only mutates
    the in-memory document.

    Args:
        project_toml_doc: A ``tomlkit`` document (mutated in place).
        patch: The compiled patch from ``compile_project_salience``.
        bundled_definition: The role's BUNDLED (package-default) definition
            — i.e. ``load_roles(code_path=None)[role_name]``, NOT the
            project-merged result of ``load_roles(code_path)``. This
            distinction is load-bearing: ``load_roles`` does whole-block
            replace per role (``role_loader.py::load_roles``,
            ``merged.update(overrides)``), so a project's *partial*
            override block already loads with its omitted fields blanked
            to dataclass defaults, not inherited from the bundled catalog.
            Regenerating a partial block from that already-blanked merged
            value would silently erase the bundled prose for every field
            the project did NOT explicitly set. To regenerate correctly,
            this function reads each field's value from the **raw existing
            block** when the project explicitly set it, and falls back to
            ``bundled_definition`` only for fields the project never set —
            never from a merged/flattened value.
    """
    if not patch.has_changes:
        return

    import tomlkit

    mode = classify_write_mode(patch.role, project_toml_doc)
    bullets = [b.text for b in patch.accepted]

    roles_table = project_toml_doc.get("roles")
    if roles_table is None:
        roles_table = tomlkit.table()
        project_toml_doc["roles"] = roles_table

    if mode == "complete_override":
        # In-place key update — preserves every other field and the
        # surrounding comments (tomlkit round-trip).
        roles_table[patch.role]["project_salience"] = bullets
        return

    # partial_override or bundled_only: regenerate the COMPLETE block,
    # preserving each field's EXPLICIT project value when the existing
    # block already set it, falling back to the bundled default only for
    # fields the project never set. See the docstring above for why this
    # must read the raw block rather than a merged RoleDefinition.
    existing_block = roles_table.get(patch.role)
    existing_keys = (
        set(existing_block.keys())
        if existing_block is not None and hasattr(existing_block, "keys")
        else set()
    )

    def _field(name: str, bundled_value):
        if existing_block is not None and name in existing_keys:
            return existing_block[name]
        return bundled_value

    new_block = tomlkit.table()
    new_block["description"] = _field("description", bundled_definition.description)
    new_block["canonical_role"] = _field(
        "canonical_role", bundled_definition.canonical_role
    )
    new_block["produces"] = _field("produces", bundled_definition.produces)
    new_block["boundary"] = _field("boundary", bundled_definition.boundary)
    new_block["handoff_to"] = _field("handoff_to", bundled_definition.handoff_to)
    new_block["instructions"] = _field(
        "instructions", bundled_definition.instructions
    )
    new_block["entry_style"] = _field("entry_style", bundled_definition.entry_style)
    new_block["when_to_use"] = _field("when_to_use", bundled_definition.when_to_use)
    new_block["collaborate_with"] = _field(
        "collaborate_with", bundled_definition.collaborate_with
    )
    new_block["project_salience"] = bullets
    roles_table[patch.role] = new_block


# ---------------------------------------------------------------------------
# Projection ledger (enables Phase 4 retirement)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectionLedgerEntry:
    """Per-bullet provenance record, external to the committed roles.toml.

    Keyed by role + normalized-text hash + source Lesson ULID, per the
    plan's Phase 2 design. Not read on the daemon's hot emission path —
    queryable on demand by the retirement workflow (Phase 4) and by humans
    auditing where a bullet came from.
    """

    role: str
    text: str
    text_hash: str
    source_lesson_ulid: str
    authority_basis: str
    confidence: Optional[float]
    review_after: Optional[str]
    status: Literal["active", "retired", "superseded"]
    projected_at: float


def bullet_text_hash(role: str, text: str) -> str:
    """The ledger's per-bullet hash key: role + normalized bullet text.

    Uses the same ``normalize_salience_bullet`` normalization as the stance
    daemon's signature hash (``pulse_stance_lib.py``), so a ledger record
    and the bullet's runtime contribution to a stance advisory's dedup
    signature are reproducibly linkable.
    """
    normalized = normalize_salience_bullet(text)
    return hashlib.sha256(f"{role}::{normalized}".encode("utf-8")).hexdigest()[:16]


def ledger_entry_for(
    bullet: SalienceBullet, role: str, *, projected_at: Optional[float] = None
) -> ProjectionLedgerEntry:
    """Build a ledger entry for one accepted bullet."""
    return ProjectionLedgerEntry(
        role=role,
        text=bullet.text,
        text_hash=bullet_text_hash(role, bullet.text),
        source_lesson_ulid=bullet.source_lesson_ulid,
        authority_basis="human_promoted_lesson",
        confidence=bullet.confidence,
        review_after=bullet.review_after,
        status="active",
        projected_at=projected_at if projected_at is not None else time.time(),
    )


def append_ledger_entries(
    ledger_path: Path, entries: list[ProjectionLedgerEntry]
) -> None:
    """Append ledger entries as JSONL, creating the file/parent dirs if needed.

    A no-op for an empty ``entries`` list (does not even create the file) —
    the "nothing to compile" clean path must not leave an empty ledger
    artifact behind.
    """
    if not entries:
        return
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(asdict(entry)) + "\n")


def load_ledger_entries(ledger_path: Path) -> list[ProjectionLedgerEntry]:
    """Read all ledger entries from a JSONL file.

    Returns an empty list if the file does not exist. Malformed lines are
    skipped (the ledger is an append-only audit trail, not a source the
    compile/write path depends on for correctness — a corrupt line should
    not block compilation). A line that parses as JSON and constructs a
    ``ProjectionLedgerEntry`` (no missing/extra keys) but carries a
    ``status`` outside ``_VALID_LEDGER_STATUSES`` is also skipped — the
    dataclass itself does not enforce ``Literal`` at runtime, so this is an
    explicit guard rather than relying on downstream lookups happening to
    fail closed for an unrecognized status.
    """
    if not ledger_path.is_file():
        return []
    entries: list[ProjectionLedgerEntry] = []
    with ledger_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                entry = ProjectionLedgerEntry(**record)
            except (json.JSONDecodeError, TypeError):
                continue
            if entry.status not in _VALID_LEDGER_STATUSES:
                continue
            entries.append(entry)
    return entries


@dataclass(frozen=True)
class ProvenanceReport:
    """Result of auditing a role's committed ``project_salience`` bullets
    against the projection ledger.

    ``unledgered`` bullets are not necessarily wrong — they may predate the
    ledger, or were hand-authored directly (which the loader always
    permits; the ledger documents the compiler's provenance, it does not
    gate the loader). They are surfaced so a human reviewing a future
    ``update-roles-context`` run can see which bullets in the committed
    contract carry no audit trail, closing the authority loop named in the
    plan's "Authority & provenance model": the human gate is the git-landed
    file, and this report makes what *isn't* ledger-backed visible at the
    next compile/land point rather than silently accumulating.
    """

    role: str
    ledgered: tuple[str, ...]
    unledgered: tuple[str, ...]

    @property
    def has_unledgered(self) -> bool:
        return bool(self.unledgered)


def verify_ledger_provenance(
    definition: RoleDefinition, ledger_path: Path
) -> ProvenanceReport:
    """Classify a role's committed ``project_salience`` bullets as
    ledgered (an ``active``-status ledger record exists for that bullet) or
    unledgered (no record, or the latest record's status is not
    ``active``).

    This is a **compile/land-time** check — the caller (the
    ``update-roles-context`` skill) runs it before proposing new patches and
    surfaces the result in its report. It does not gate the loader or any
    runtime daemon path (the plan explicitly defers a runtime ledger-join;
    this function implements the lighter-weight compile-time alternative
    named in the PR-slicing table as "closes the authority loop").
    """
    role = definition.name
    all_entries = load_ledger_entries(ledger_path)

    # Latest entry per text_hash determines current status (the ledger is
    # append-only; a later record supersedes an earlier one for the same
    # bullet's hash).
    latest_status_by_hash: dict[str, str] = {}
    for entry in all_entries:
        if entry.role != role:
            continue
        latest_status_by_hash[entry.text_hash] = entry.status

    ledgered: list[str] = []
    unledgered: list[str] = []
    for text in definition.project_salience:
        text_hash = bullet_text_hash(role, text)
        if latest_status_by_hash.get(text_hash) == "active":
            ledgered.append(text)
        else:
            unledgered.append(text)

    return ProvenanceReport(
        role=role, ledgered=tuple(ledgered), unledgered=tuple(unledgered)
    )


# ---------------------------------------------------------------------------
# Retirement / decay (Phase 4 — no salience bullet becomes permanent)
# ---------------------------------------------------------------------------

RetirementReason = Literal["review_after_passed", "superseded", "explicit_removal"]


@dataclass(frozen=True)
class StaleBullet:
    """A committed bullet flagged for retirement review."""

    text: str
    reason: RetirementReason
    review_after: Optional[str]


def find_review_due_bullets(
    definition: RoleDefinition,
    ledger_path: Path,
    *,
    today: Optional[date] = None,
) -> list[StaleBullet]:
    """Find committed bullets whose ledger ``review_after`` date has passed.

    A bullet with no matching **active** ledger record (unledgered, per
    ``verify_ledger_provenance``) or no ``review_after`` set is not flagged
    here — this function only implements the ``review_after`` retirement
    mechanism. Malformed ``review_after`` values (not ISO ``YYYY-MM-DD``)
    are skipped rather than raising — a bad date should not crash the
    retirement scan; ``update-roles-context`` surfaces it separately if
    needed.

    Args:
        definition: The role's current definition (its committed
            ``project_salience`` bullets are what gets scanned).
        ledger_path: Path to the projection ledger JSONL.
        today: Reference date for "has this passed". Defaults to
            ``date.today()``; pass explicitly for deterministic tests.
    """
    role = definition.name
    cutoff = today if today is not None else date.today()
    all_entries = load_ledger_entries(ledger_path)

    latest_by_hash: dict[str, ProjectionLedgerEntry] = {}
    for entry in all_entries:
        if entry.role != role:
            continue
        latest_by_hash[entry.text_hash] = entry

    due: list[StaleBullet] = []
    for text in definition.project_salience:
        entry = latest_by_hash.get(bullet_text_hash(role, text))
        if entry is None or entry.status != "active" or not entry.review_after:
            continue
        try:
            review_after_date = date.fromisoformat(entry.review_after)
        except ValueError:
            continue
        if review_after_date <= cutoff:
            due.append(
                StaleBullet(
                    text=text,
                    reason="review_after_passed",
                    review_after=entry.review_after,
                )
            )
    return due


def build_retirement_patch(
    definition: RoleDefinition,
    retire_texts: list[str],
) -> SaliencePatch:
    """Build a ``SaliencePatch`` that removes ``retire_texts`` from a role's
    committed ``project_salience``.

    Reuses ``apply_salience_patch`` for the actual write — a retirement is
    just a patch whose ``accepted`` list is the bullets that remain.
    Retired bullets are recorded in ``dropped`` with a
    ``"retired: explicit"`` reason for the skill's report; call
    ``retire_ledger_entries`` separately to record the retirement in the
    ledger (kept separate so a caller can retire from the ledger without
    necessarily removing from ``roles.toml`` in the same step, or vice
    versa, though the normal flow does both together).

    A ``retire_texts`` entry that matches no bullet in
    ``definition.project_salience`` (e.g. a typo, or a bullet already
    retired) is **not** silently ignored — no salient truncation applies
    here just as it does for compile drops — it is recorded in ``dropped``
    with reason ``"retire_target_not_found"`` so the caller can surface the
    mismatch rather than the retirement request vanishing with no signal.
    """
    role = definition.name
    retire_set = {normalize_salience_bullet(t) for t in retire_texts}
    matched: set[str] = set()
    kept: list[SalienceBullet] = []
    dropped: list[DroppedBullet] = []
    for text in definition.project_salience:
        normalized = normalize_salience_bullet(text)
        if normalized in retire_set:
            matched.add(normalized)
            dropped.append(
                DroppedBullet(text=text, source_lesson_ulid="", reason="retired: explicit")
            )
        else:
            kept.append(SalienceBullet(text=text, source_lesson_ulid=""))

    for original_text in retire_texts:
        if normalize_salience_bullet(original_text) not in matched:
            dropped.append(
                DroppedBullet(
                    text=original_text,
                    source_lesson_ulid="",
                    reason="retire_target_not_found",
                )
            )

    return SaliencePatch(
        role=role,
        accepted=tuple(kept),
        needs_rewrite=(),
        dropped=tuple(dropped),
        has_changes=bool(matched),
    )


def retire_ledger_entries(
    ledger_path: Path,
    *,
    role: str,
    texts: list[str],
    reason: RetirementReason,
    projected_at: Optional[float] = None,
) -> list[ProjectionLedgerEntry]:
    """Append new ``retired``/``superseded`` ledger records for ``texts``.

    The ledger is append-only (Phase 2 design) — retiring a bullet never
    edits its prior ``active`` record; it appends a new record with the
    same ``text_hash`` and an updated ``status``, which ``latest entry
    wins`` semantics (``verify_ledger_provenance``,
    ``find_review_due_bullets``) then correctly treat as superseding the
    prior one. Carries forward ``source_lesson_ulid``/``confidence`` from
    the latest existing record for that bullet when one exists, so the
    retirement record still traces back to its origin.

    Returns the new entries (already appended to ``ledger_path``) for the
    caller's report — does not itself decide whether the corresponding
    bullet is removed from ``roles.toml``; pair with
    ``build_retirement_patch`` for that.
    """
    status: Literal["retired", "superseded"] = (
        "superseded" if reason == "superseded" else "retired"
    )
    existing = load_ledger_entries(ledger_path)
    latest_by_hash: dict[str, ProjectionLedgerEntry] = {}
    for entry in existing:
        if entry.role != role:
            continue
        latest_by_hash[entry.text_hash] = entry

    new_entries: list[ProjectionLedgerEntry] = []
    for text in texts:
        text_hash = bullet_text_hash(role, text)
        prior = latest_by_hash.get(text_hash)
        # A bullet with no prior ledger record (never compiled — e.g.
        # hand-authored directly in roles.toml) is being retired without
        # any provenance to carry forward. Do not fabricate
        # "human_promoted_lesson" for it — that would falsely claim this
        # bullet was ledger-tracked when it never was.
        authority_basis = prior.authority_basis if prior else "unledgered_at_retirement"
        new_entries.append(
            ProjectionLedgerEntry(
                role=role,
                text=text,
                text_hash=text_hash,
                source_lesson_ulid=prior.source_lesson_ulid if prior else "",
                authority_basis=authority_basis,
                confidence=prior.confidence if prior else None,
                review_after=prior.review_after if prior else None,
                status=status,
                projected_at=projected_at if projected_at is not None else time.time(),
            )
        )

    append_ledger_entries(ledger_path, new_entries)
    return new_entries
