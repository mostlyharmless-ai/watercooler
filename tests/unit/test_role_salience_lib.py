"""Tests for watercooler.role_salience_lib — the Role Salience Compiler's
pure compile/lint/write/ledger library."""

from __future__ import annotations

import json

import pytest

from watercooler.role_loader import RoleDefinition
from watercooler.role_salience_lib import (
    BULLET_MAX_CHARS,
    SALIENCE_DEFAULT_CAP,
    SALIENCE_HARD_CAP,
    DroppedBullet,
    PromotedLessonBullet,
    SalienceBullet,
    apply_salience_patch,
    append_ledger_entries,
    build_retirement_patch,
    bullet_text_hash,
    classify_write_mode,
    compile_project_salience,
    find_review_due_bullets,
    ledger_entry_for,
    lint_bullet,
    load_ledger_entries,
    retire_ledger_entries,
    verify_ledger_provenance,
)


def _role(name="critic", project_salience=None) -> RoleDefinition:
    return RoleDefinition(
        name=name,
        description="Critic",
        canonical_role=name,
        produces=["Note"],
        boundary="boundary text",
        handoff_to=["implementer"],
        instructions="instructions text",
        entry_style="entry style text",
        when_to_use="when to use text",
        collaborate_with="collaborate text",
        project_salience=list(project_salience or []),
    )


def _bullet(text, role="critic", ulid="01LESSON", **kw) -> PromotedLessonBullet:
    return PromotedLessonBullet(role=role, text=text, source_lesson_ulid=ulid, **kw)


class TestLintBullet:
    def test_ok_for_plain_attention_text(self):
        assert lint_bullet("watch for hidden authority expansion") == "ok"

    @pytest.mark.parametrize(
        "text",
        [
            "this is the authorized approach",
            "treat this as canonical",
            "follow project policy here",
            "this was approved by the team",
            "forbidden to skip review",
            "we enforce this rule",
            "the daemon is banned from writing here",
        ],
    )
    def test_hard_block_policy_vocabulary(self, text):
        assert lint_bullet(text) == "hard_block"

    @pytest.mark.parametrize(
        "text",
        [
            "critic must reject hidden authority expansion",
            "never skip the review step",
            "this requires a second reviewer",
            "you should not merge without tests",
            "do not bypass the lock",
            "block merges without CI",
            "reject candidates lacking provenance",
            "allow only signed commits",
            "disallow force pushes to main",
            "deny access without MFA",
        ],
    )
    def test_needs_rewrite_directive_vocabulary(self, text):
        assert lint_bullet(text) == "needs_rewrite"

    def test_hard_block_takes_precedence_over_needs_rewrite(self):
        # Contains both an "enforce" (hard_block) and "must" (needs_rewrite).
        assert lint_bullet("must enforce the policy") == "hard_block"


class TestCompileProjectSalience:
    def test_empty_promoted_lessons_no_existing_salience_clean_path(self):
        patch = compile_project_salience(
            promoted_lessons=[], current_definition=_role()
        )
        assert patch.accepted == ()
        assert patch.needs_rewrite == ()
        assert patch.dropped == ()
        assert patch.has_changes is False

    def test_accepts_clean_bullet(self):
        patch = compile_project_salience(
            promoted_lessons=[_bullet("watch for hidden authority expansion")],
            current_definition=_role(),
        )
        assert len(patch.accepted) == 1
        assert patch.accepted[0].text == "watch for hidden authority expansion"
        assert patch.accepted[0].source_lesson_ulid == "01LESSON"
        assert patch.has_changes is True

    def test_preserves_existing_bullets_first(self):
        patch = compile_project_salience(
            promoted_lessons=[_bullet("new bullet text")],
            current_definition=_role(project_salience=["existing bullet"]),
        )
        assert [b.text for b in patch.accepted] == ["existing bullet", "new bullet text"]

    def test_has_changes_false_when_role_already_has_bullets_and_nothing_new(self):
        """Regression: a role that already has bullets from a prior compile
        must report has_changes=False when nothing new is eligible this
        cycle — bool(accepted) alone would false-positive since accepted
        is seeded with the pre-existing bullets."""
        patch = compile_project_salience(
            promoted_lessons=[_bullet("this is canonical")],  # hard_block, dropped
            current_definition=_role(
                project_salience=["existing one", "existing two", "existing three"]
            ),
        )
        assert [b.text for b in patch.accepted] == [
            "existing one",
            "existing two",
            "existing three",
        ]
        assert patch.has_changes is False

    def test_has_changes_true_when_new_bullet_actually_added(self):
        patch = compile_project_salience(
            promoted_lessons=[_bullet("new bullet text")],
            current_definition=_role(project_salience=["existing bullet"]),
        )
        assert patch.has_changes is True

    def test_has_changes_false_with_no_existing_and_nothing_new(self):
        patch = compile_project_salience(
            promoted_lessons=[], current_definition=_role(project_salience=[])
        )
        assert patch.has_changes is False

    def test_cap_never_drops_existing_committed_bullets(self):
        """The cap applies only to NEW additions — existing committed bullets
        are never dropped by it. When a role already sits at/over the cap and
        a new candidate arrives, the new one is dropped (cap_exceeded) and
        every existing bullet is preserved. (Regression: the compiler used to
        trim existing bullets, proposing deletion of hand-authored content.)"""
        existing = [f"existing {i}" for i in range(SALIENCE_DEFAULT_CAP)]
        patch = compile_project_salience(
            promoted_lessons=[_bullet("new one")],
            current_definition=_role(project_salience=existing),
            default_cap=SALIENCE_DEFAULT_CAP - 1,
        )
        assert [b.text for b in patch.accepted] == existing
        assert all(d.text != "new one" or "cap_exceeded" in d.reason for d in patch.dropped)
        assert any(d.text == "new one" for d in patch.dropped)
        # No existing bullet is ever in `dropped`.
        assert not any(d.text in existing for d in patch.dropped)

    def test_no_op_run_over_cap_reports_no_changes(self):
        """A role legitimately carrying more than default_cap bullets (allowed
        up to SALIENCE_HARD_CAP) compiles to has_changes=False on a run with
        zero new candidates — it must not propose deleting the human's own
        content."""
        existing = [f"existing cue {i}" for i in range(SALIENCE_DEFAULT_CAP + 1)]
        patch = compile_project_salience(
            promoted_lessons=[],
            current_definition=_role(project_salience=existing),
            default_cap=SALIENCE_DEFAULT_CAP,
        )
        assert patch.has_changes is False
        assert [b.text for b in patch.accepted] == existing
        assert patch.dropped == ()

    def test_hard_block_dropped_with_reason(self):
        patch = compile_project_salience(
            promoted_lessons=[_bullet("this is the canonical approach")],
            current_definition=_role(),
        )
        assert patch.accepted == ()
        assert len(patch.dropped) == 1
        assert "hard_block" in patch.dropped[0].reason

    def test_needs_rewrite_flagged_not_written(self):
        patch = compile_project_salience(
            promoted_lessons=[_bullet("critic must reject hidden authority")],
            current_definition=_role(),
        )
        assert patch.accepted == ()
        assert len(patch.needs_rewrite) == 1
        assert patch.dropped == ()

    def test_empty_text_dropped(self):
        patch = compile_project_salience(
            promoted_lessons=[_bullet("   ")], current_definition=_role()
        )
        assert patch.accepted == ()
        assert patch.dropped[0].reason == "empty"

    def test_overlength_bullet_dropped(self):
        long_text = "x" * (BULLET_MAX_CHARS + 1)
        patch = compile_project_salience(
            promoted_lessons=[_bullet(long_text)], current_definition=_role()
        )
        assert patch.accepted == ()
        assert "too_long" in patch.dropped[0].reason

    def test_exactly_max_length_accepted(self):
        text = "x" * BULLET_MAX_CHARS
        patch = compile_project_salience(
            promoted_lessons=[_bullet(text)], current_definition=_role()
        )
        assert len(patch.accepted) == 1

    def test_duplicate_against_existing_dropped(self):
        patch = compile_project_salience(
            promoted_lessons=[_bullet("watch for X")],
            current_definition=_role(project_salience=["watch for X"]),
        )
        assert len(patch.accepted) == 1  # only the existing one
        assert patch.dropped[0].reason == "duplicate"

    def test_duplicate_normalization_insensitive(self):
        """Whitespace-only differences count as the same bullet (same
        normalization the stance daemon signature uses)."""
        patch = compile_project_salience(
            promoted_lessons=[_bullet("  watch   for   X  ")],
            current_definition=_role(project_salience=["watch for X"]),
        )
        assert patch.dropped[0].reason == "duplicate"

    def test_duplicate_among_new_candidates_dropped(self):
        patch = compile_project_salience(
            promoted_lessons=[_bullet("watch for X"), _bullet("watch for X", ulid="02LESSON")],
            current_definition=_role(),
        )
        assert len(patch.accepted) == 1
        assert patch.dropped[0].reason == "duplicate"

    def test_cap_exceeded_drops_overflow_no_silent_truncation(self):
        lessons = [_bullet(f"bullet {i}", ulid=f"L{i}") for i in range(SALIENCE_DEFAULT_CAP + 2)]
        patch = compile_project_salience(
            promoted_lessons=lessons, current_definition=_role()
        )
        assert len(patch.accepted) == SALIENCE_DEFAULT_CAP
        assert len(patch.dropped) == 2
        assert all("cap_exceeded" in d.reason for d in patch.dropped)

    def test_existing_bullets_count_toward_cap(self):
        existing = [f"existing {i}" for i in range(SALIENCE_DEFAULT_CAP)]
        patch = compile_project_salience(
            promoted_lessons=[_bullet("new one")],
            current_definition=_role(project_salience=existing),
        )
        assert len(patch.accepted) == SALIENCE_DEFAULT_CAP
        assert patch.dropped[0].reason.startswith("cap_exceeded")

    def test_default_cap_override_within_hard_cap(self):
        lessons = [_bullet(f"bullet {i}", ulid=f"L{i}") for i in range(SALIENCE_HARD_CAP)]
        patch = compile_project_salience(
            promoted_lessons=lessons,
            current_definition=_role(),
            default_cap=SALIENCE_HARD_CAP,
        )
        assert len(patch.accepted) == SALIENCE_HARD_CAP
        assert patch.dropped == ()

    def test_default_cap_above_hard_cap_raises(self):
        with pytest.raises(ValueError, match="SALIENCE_HARD_CAP"):
            compile_project_salience(
                promoted_lessons=[],
                current_definition=_role(),
                default_cap=SALIENCE_HARD_CAP + 1,
            )

    def test_role_mismatch_raises(self):
        with pytest.raises(ValueError, match="does not match"):
            compile_project_salience(
                promoted_lessons=[_bullet("x", role="planner")],
                current_definition=_role(name="critic"),
            )

    def test_ordering_preserved_existing_then_input_order(self):
        patch = compile_project_salience(
            promoted_lessons=[
                _bullet("second", ulid="L2"),
                _bullet("third", ulid="L3"),
            ],
            current_definition=_role(project_salience=["first"]),
        )
        assert [b.text for b in patch.accepted] == ["first", "second", "third"]


class TestClassifyWriteMode:
    def test_bundled_only_when_no_roles_table(self):
        import tomlkit

        doc = tomlkit.document()
        assert classify_write_mode("critic", doc) == "bundled_only"

    def test_bundled_only_when_role_absent(self):
        import tomlkit

        doc = tomlkit.parse("[roles.planner]\ndescription = \"x\"\n")
        assert classify_write_mode("critic", doc) == "bundled_only"

    def test_complete_override_when_all_fields_present(self):
        import tomlkit

        doc = tomlkit.parse(
            "[roles.critic]\n"
            'description = "d"\n'
            'canonical_role = "critic"\n'
            "produces = []\n"
            'boundary = "b"\n'
            "handoff_to = []\n"
            'instructions = "i"\n'
            'entry_style = "e"\n'
            'when_to_use = "w"\n'
            'collaborate_with = "c"\n'
        )
        assert classify_write_mode("critic", doc) == "complete_override"

    def test_partial_override_when_fields_missing(self):
        import tomlkit

        doc = tomlkit.parse(
            '[roles.critic]\ndescription = "d"\ncanonical_role = "critic"\n'
        )
        assert classify_write_mode("critic", doc) == "partial_override"

    def test_partial_override_salience_only_block(self):
        """The exact scenario the loader's whole-block-replace makes
        load-bearing: a block that already carries only project_salience."""
        import tomlkit

        doc = tomlkit.parse('[roles.critic]\nproject_salience = ["x"]\n')
        assert classify_write_mode("critic", doc) == "partial_override"


class TestApplySalienceePatch:
    def test_no_op_when_no_changes(self):
        import tomlkit

        doc = tomlkit.parse('[roles.critic]\ndescription = "d"\n')
        before = tomlkit.dumps(doc)
        patch = compile_project_salience(
            promoted_lessons=[], current_definition=_role()
        )
        apply_salience_patch(
            project_toml_doc=doc, patch=patch, bundled_definition=_role()
        )
        assert tomlkit.dumps(doc) == before

    def test_complete_override_updates_key_in_place_preserves_others(self):
        import tomlkit

        doc = tomlkit.parse(
            "[roles.critic]\n"
            '# a custom comment\n'
            'description = "Custom critic description"\n'
            'canonical_role = "critic"\n'
            "produces = []\n"
            'boundary = "b"\n'
            "handoff_to = []\n"
            'instructions = "i"\n'
            'entry_style = "e"\n'
            'when_to_use = "w"\n'
            'collaborate_with = "c"\n'
        )
        merged = _role()
        patch = compile_project_salience(
            promoted_lessons=[_bullet("watch for X")], current_definition=merged
        )
        apply_salience_patch(
            project_toml_doc=doc, patch=patch, bundled_definition=merged
        )
        rendered = tomlkit.dumps(doc)
        assert "Custom critic description" in rendered  # preserved value
        assert "# a custom comment" in rendered  # preserved comment
        assert 'project_salience = ["watch for X"]' in rendered

    def test_partial_override_regenerates_complete_block_from_bundled_fallback(self):
        """A partial block's omitted fields must come from the BUNDLED
        definition, not be blanked — even though current_definition (what
        compile_project_salience reads) is the merged/already-blanked value
        for any field the partial block didn't set."""
        import tomlkit

        doc = tomlkit.parse('[roles.critic]\ndescription = "Custom description"\n')
        bundled = _role(name="critic")  # stands in for the real bundled catalog
        # Simulates load_roles()'s actual whole-block-replace result for this
        # partial block: only `description` survives, everything else is
        # blanked to dataclass defaults — NOT inherited from bundled.
        merged_as_loaded = RoleDefinition(
            name="critic", description="Custom description", canonical_role="critic"
        )
        patch = compile_project_salience(
            promoted_lessons=[_bullet("watch for X")],
            current_definition=merged_as_loaded,
        )
        apply_salience_patch(
            project_toml_doc=doc, patch=patch, bundled_definition=bundled
        )
        rendered = tomlkit.dumps(doc)
        # The explicit project override must survive.
        assert "Custom description" in rendered
        # Every field the partial block did NOT set must fall back to the
        # BUNDLED value, not the already-blanked merged value.
        for field_value in (
            bundled.boundary,
            bundled.instructions,
            bundled.entry_style,
            bundled.when_to_use,
            bundled.collaborate_with,
        ):
            assert field_value in rendered
        assert 'project_salience = ["watch for X"]' in rendered

    def test_partial_override_real_load_roles_regression(self, tmp_path):
        """Regression test using the REAL load_roles() merge path (not a
        hand-built stand-in), per the reviewed gap: a project that adds a
        one-field partial override must not have its bundled prose erased
        by a salience compile run."""
        import tomlkit

        from watercooler.role_loader import load_roles

        wc_dir = tmp_path / ".watercooler"
        wc_dir.mkdir()
        roles_path = wc_dir / "roles.toml"
        roles_path.write_text('[roles.critic]\ndescription = "Custom description"\n')

        bundled = load_roles(code_path=None)["critic"]
        merged = load_roles(code_path=tmp_path)["critic"]
        # Confirms the documented whole-block-replace hazard is real: the
        # merged value already has bundled prose blanked for this partial
        # block, which is exactly why apply_salience_patch must NOT use it
        # as its regeneration source.
        assert merged.boundary == ""
        assert bundled.boundary != ""

        patch = compile_project_salience(
            promoted_lessons=[_bullet("watch for X")], current_definition=merged
        )
        doc = tomlkit.parse(roles_path.read_text())
        apply_salience_patch(
            project_toml_doc=doc, patch=patch, bundled_definition=bundled
        )
        roles_path.write_text(tomlkit.dumps(doc))

        reloaded = load_roles(code_path=tmp_path)["critic"]
        assert reloaded.description == "Custom description"  # explicit override kept
        assert reloaded.boundary == bundled.boundary  # bundled fallback, not blank
        assert reloaded.instructions == bundled.instructions
        assert reloaded.project_salience == ["watch for X"]

    def test_bundled_only_generates_complete_block(self):
        import tomlkit

        doc = tomlkit.document()
        merged = _role()
        patch = compile_project_salience(
            promoted_lessons=[_bullet("watch for X")], current_definition=merged
        )
        apply_salience_patch(
            project_toml_doc=doc, patch=patch, bundled_definition=merged
        )
        rendered = tomlkit.dumps(doc)
        assert "[roles.critic]" in rendered or "critic" in doc["roles"]
        assert 'project_salience = ["watch for X"]' in rendered


class TestProjectionLedger:
    def test_ledger_entry_for_shape(self):
        bullet = SalienceBullet(
            text="watch for X", source_lesson_ulid="01LESSON", confidence=4.0
        )
        entry = ledger_entry_for(bullet, "critic", projected_at=1000.0)
        assert entry.role == "critic"
        assert entry.text == "watch for X"
        assert entry.source_lesson_ulid == "01LESSON"
        assert entry.authority_basis == "human_promoted_lesson"
        assert entry.status == "active"
        assert entry.projected_at == 1000.0
        assert len(entry.text_hash) == 16

    def test_ledger_hash_reproducible_across_whitespace_variants(self):
        b1 = SalienceBullet(text="watch for X", source_lesson_ulid="L1")
        b2 = SalienceBullet(text="  watch   for   X  ", source_lesson_ulid="L2")
        e1 = ledger_entry_for(b1, "critic", projected_at=1.0)
        e2 = ledger_entry_for(b2, "critic", projected_at=2.0)
        assert e1.text_hash == e2.text_hash

    def test_ledger_hash_differs_by_role(self):
        bullet = SalienceBullet(text="watch for X", source_lesson_ulid="L1")
        e1 = ledger_entry_for(bullet, "critic", projected_at=1.0)
        e2 = ledger_entry_for(bullet, "planner", projected_at=1.0)
        assert e1.text_hash != e2.text_hash

    def test_append_ledger_entries_writes_jsonl(self, tmp_path):
        ledger_path = tmp_path / "ledger.jsonl"
        bullet = SalienceBullet(text="watch for X", source_lesson_ulid="01LESSON")
        entry = ledger_entry_for(bullet, "critic", projected_at=1000.0)
        append_ledger_entries(ledger_path, [entry])

        lines = ledger_path.read_text().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["role"] == "critic"
        assert record["text"] == "watch for X"
        assert record["status"] == "active"

    def test_append_ledger_entries_appends_not_overwrites(self, tmp_path):
        ledger_path = tmp_path / "ledger.jsonl"
        b1 = ledger_entry_for(
            SalienceBullet(text="a", source_lesson_ulid="L1"), "critic", projected_at=1.0
        )
        b2 = ledger_entry_for(
            SalienceBullet(text="b", source_lesson_ulid="L2"), "critic", projected_at=2.0
        )
        append_ledger_entries(ledger_path, [b1])
        append_ledger_entries(ledger_path, [b2])
        lines = ledger_path.read_text().splitlines()
        assert len(lines) == 2

    def test_append_ledger_entries_empty_list_creates_no_file(self, tmp_path):
        ledger_path = tmp_path / "ledger.jsonl"
        append_ledger_entries(ledger_path, [])
        assert not ledger_path.exists()


class TestEndToEndCompileAndWrite:
    """Phase 2 acceptance: compiles promoted Lessons → reviewable patch;
    lint blocks policy verbs; patched block preserves every non-salience
    field; ledger record written per bullet; clean 'nothing to compile' path."""

    def test_full_flow_writes_ledger_and_patch(self, tmp_path):
        import tomlkit

        merged = _role(project_salience=["existing bullet"])
        lessons = [
            _bullet("watch for hidden authority expansion", ulid="L1"),
            _bullet("this is canonical", ulid="L2"),  # hard_block
            _bullet("must enforce X", ulid="L3"),  # hard_block (enforce wins)
        ]
        patch = compile_project_salience(
            promoted_lessons=lessons, current_definition=merged
        )
        assert len(patch.accepted) == 2  # existing + 1 new
        assert len(patch.dropped) == 2

        doc = tomlkit.parse('[roles.critic]\ndescription = "Custom"\n')
        apply_salience_patch(project_toml_doc=doc, patch=patch, bundled_definition=merged)
        rendered = tomlkit.dumps(doc)
        assert "existing bullet" in rendered
        assert "watch for hidden authority expansion" in rendered
        assert "this is canonical" not in rendered
        assert "must enforce X" not in rendered

        ledger_path = tmp_path / "ledger.jsonl"
        new_entries = [
            ledger_entry_for(b, patch.role)
            for b in patch.accepted
            if b.source_lesson_ulid  # only newly-projected bullets, not pre-existing
        ]
        append_ledger_entries(ledger_path, new_entries)
        lines = ledger_path.read_text().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["source_lesson_ulid"] == "L1"


class TestLoadLedgerEntries:
    def test_missing_file_returns_empty(self, tmp_path):
        assert load_ledger_entries(tmp_path / "nonexistent.jsonl") == []

    def test_round_trips_entries(self, tmp_path):
        ledger_path = tmp_path / "ledger.jsonl"
        bullet = SalienceBullet(text="watch for X", source_lesson_ulid="L1")
        entry = ledger_entry_for(bullet, "critic", projected_at=1000.0)
        append_ledger_entries(ledger_path, [entry])

        loaded = load_ledger_entries(ledger_path)
        assert len(loaded) == 1
        assert loaded[0] == entry

    def test_skips_malformed_lines(self, tmp_path):
        from dataclasses import asdict

        ledger_path = tmp_path / "ledger.jsonl"
        bullet = SalienceBullet(text="watch for X", source_lesson_ulid="L1")
        entry = ledger_entry_for(bullet, "critic", projected_at=1000.0)
        ledger_path.write_text("not json\n" + json.dumps(asdict(entry)) + "\n")
        loaded = load_ledger_entries(ledger_path)
        assert len(loaded) == 1

    def test_skips_valid_json_with_unknown_status(self, tmp_path):
        """A JSON-valid line that constructs a ProjectionLedgerEntry with no
        missing/extra keys but an out-of-range status must still be
        skipped — the dataclass does not enforce Literal at runtime."""
        from dataclasses import asdict

        ledger_path = tmp_path / "ledger.jsonl"
        bullet = SalienceBullet(text="watch for X", source_lesson_ulid="L1")
        entry = ledger_entry_for(bullet, "critic", projected_at=1000.0)
        bad_record = asdict(entry)
        bad_record["status"] = "pending"  # not in _VALID_LEDGER_STATUSES
        ledger_path.write_text(json.dumps(bad_record) + "\n")
        loaded = load_ledger_entries(ledger_path)
        assert loaded == []

    def test_skips_missing_required_key(self, tmp_path):
        ledger_path = tmp_path / "ledger.jsonl"
        ledger_path.write_text(json.dumps({"role": "critic"}) + "\n")
        assert load_ledger_entries(ledger_path) == []


class TestVerifyLedgerProvenance:
    def test_no_ledger_file_all_unledgered(self, tmp_path):
        definition = _role(project_salience=["watch for X", "watch for Y"])
        report = verify_ledger_provenance(definition, tmp_path / "ledger.jsonl")
        assert report.ledgered == ()
        assert set(report.unledgered) == {"watch for X", "watch for Y"}
        assert report.has_unledgered is True

    def test_no_bullets_clean_report(self, tmp_path):
        definition = _role(project_salience=[])
        report = verify_ledger_provenance(definition, tmp_path / "ledger.jsonl")
        assert report.ledgered == ()
        assert report.unledgered == ()
        assert report.has_unledgered is False

    def test_ledgered_bullet_classified_correctly(self, tmp_path):
        ledger_path = tmp_path / "ledger.jsonl"
        bullet = SalienceBullet(text="watch for X", source_lesson_ulid="L1")
        entry = ledger_entry_for(bullet, "critic", projected_at=1000.0)
        append_ledger_entries(ledger_path, [entry])

        definition = _role(project_salience=["watch for X", "watch for Y"])
        report = verify_ledger_provenance(definition, ledger_path)
        assert report.ledgered == ("watch for X",)
        assert report.unledgered == ("watch for Y",)

    def test_normalization_insensitive_match(self, tmp_path):
        """A ledger entry for "watch for X" still matches a committed bullet
        with different whitespace — same hash via normalize_salience_bullet."""
        ledger_path = tmp_path / "ledger.jsonl"
        bullet = SalienceBullet(text="watch   for   X", source_lesson_ulid="L1")
        entry = ledger_entry_for(bullet, "critic", projected_at=1000.0)
        append_ledger_entries(ledger_path, [entry])

        definition = _role(project_salience=["watch for X"])
        report = verify_ledger_provenance(definition, ledger_path)
        assert report.ledgered == ("watch for X",)

    def test_retired_status_treated_as_unledgered(self, tmp_path):
        ledger_path = tmp_path / "ledger.jsonl"
        bullet = SalienceBullet(text="watch for X", source_lesson_ulid="L1")
        entry = ledger_entry_for(bullet, "critic", projected_at=1000.0)
        from dataclasses import replace

        retired = replace(entry, status="retired")
        append_ledger_entries(ledger_path, [retired])

        definition = _role(project_salience=["watch for X"])
        report = verify_ledger_provenance(definition, ledger_path)
        assert report.unledgered == ("watch for X",)
        assert report.ledgered == ()

    def test_latest_status_wins_over_earlier_entry(self, tmp_path):
        """An active record followed by a later retired record for the same
        bullet must classify as unledgered (the ledger is append-only;
        later entries supersede earlier ones)."""
        ledger_path = tmp_path / "ledger.jsonl"
        bullet = SalienceBullet(text="watch for X", source_lesson_ulid="L1")
        active = ledger_entry_for(bullet, "critic", projected_at=1000.0)
        from dataclasses import replace

        retired_later = replace(active, projected_at=2000.0, status="retired")
        append_ledger_entries(ledger_path, [active, retired_later])

        definition = _role(project_salience=["watch for X"])
        report = verify_ledger_provenance(definition, ledger_path)
        assert report.unledgered == ("watch for X",)

    def test_cross_role_ledger_entries_do_not_match(self, tmp_path):
        """A ledger entry for planner must not provenance a critic bullet
        with the same text — bullet_text_hash is per-role."""
        ledger_path = tmp_path / "ledger.jsonl"
        bullet = SalienceBullet(text="watch for X", source_lesson_ulid="L1")
        entry = ledger_entry_for(bullet, "planner", projected_at=1000.0)
        append_ledger_entries(ledger_path, [entry])

        definition = _role(name="critic", project_salience=["watch for X"])
        report = verify_ledger_provenance(definition, ledger_path)
        assert report.unledgered == ("watch for X",)

    def test_bundled_only_role_no_ledger_no_crash(self, tmp_path):
        """A role with no project_salience at all (bundled default) and no
        ledger file produces a clean empty report, not an error."""
        definition = _role(project_salience=[])
        report = verify_ledger_provenance(definition, tmp_path / "missing.jsonl")
        assert report.has_unledgered is False


class TestBulletTextHash:
    def test_matches_ledger_entry_hash(self):
        bullet = SalienceBullet(text="watch for X", source_lesson_ulid="L1")
        entry = ledger_entry_for(bullet, "critic", projected_at=1.0)
        assert bullet_text_hash("critic", "watch for X") == entry.text_hash

    def test_normalization_insensitive(self):
        assert bullet_text_hash("critic", "watch for X") == bullet_text_hash(
            "critic", "  watch   for   X  "
        )

    def test_role_scoped(self):
        assert bullet_text_hash("critic", "x") != bullet_text_hash("planner", "x")


class TestFindReviewDueBullets:
    def test_no_ledger_no_bullets_empty(self, tmp_path):
        definition = _role(project_salience=[])
        assert find_review_due_bullets(definition, tmp_path / "ledger.jsonl") == []

    def test_bullet_with_no_review_after_not_flagged(self, tmp_path):
        ledger_path = tmp_path / "ledger.jsonl"
        bullet = SalienceBullet(text="watch for X", source_lesson_ulid="L1")
        append_ledger_entries(ledger_path, [ledger_entry_for(bullet, "critic")])
        definition = _role(project_salience=["watch for X"])
        assert find_review_due_bullets(definition, ledger_path) == []

    def test_review_after_in_future_not_flagged(self, tmp_path):
        from datetime import date

        ledger_path = tmp_path / "ledger.jsonl"
        bullet = SalienceBullet(
            text="watch for X", source_lesson_ulid="L1", review_after="2099-01-01"
        )
        append_ledger_entries(ledger_path, [ledger_entry_for(bullet, "critic")])
        definition = _role(project_salience=["watch for X"])
        due = find_review_due_bullets(
            definition, ledger_path, today=date(2026, 6, 30)
        )
        assert due == []

    def test_review_after_passed_flagged(self, tmp_path):
        from datetime import date

        ledger_path = tmp_path / "ledger.jsonl"
        bullet = SalienceBullet(
            text="watch for X", source_lesson_ulid="L1", review_after="2026-01-01"
        )
        append_ledger_entries(ledger_path, [ledger_entry_for(bullet, "critic")])
        definition = _role(project_salience=["watch for X"])
        due = find_review_due_bullets(
            definition, ledger_path, today=date(2026, 6, 30)
        )
        assert len(due) == 1
        assert due[0].text == "watch for X"
        assert due[0].reason == "review_after_passed"
        assert due[0].review_after == "2026-01-01"

    def test_review_after_exactly_today_flagged(self, tmp_path):
        from datetime import date

        ledger_path = tmp_path / "ledger.jsonl"
        bullet = SalienceBullet(
            text="watch for X", source_lesson_ulid="L1", review_after="2026-06-30"
        )
        append_ledger_entries(ledger_path, [ledger_entry_for(bullet, "critic")])
        definition = _role(project_salience=["watch for X"])
        due = find_review_due_bullets(
            definition, ledger_path, today=date(2026, 6, 30)
        )
        assert len(due) == 1

    def test_malformed_review_after_skipped_not_crash(self, tmp_path):
        ledger_path = tmp_path / "ledger.jsonl"
        bullet = SalienceBullet(
            text="watch for X", source_lesson_ulid="L1", review_after="not-a-date"
        )
        append_ledger_entries(ledger_path, [ledger_entry_for(bullet, "critic")])
        definition = _role(project_salience=["watch for X"])
        assert find_review_due_bullets(definition, ledger_path) == []

    def test_unledgered_bullet_not_flagged(self, tmp_path):
        """A bullet with no matching active ledger record is out of scope
        for review_after retirement — it's a provenance-audit concern, not
        a decay concern."""
        definition = _role(project_salience=["hand-authored bullet"])
        assert find_review_due_bullets(definition, tmp_path / "ledger.jsonl") == []

    def test_retired_bullet_not_reflagged(self, tmp_path):
        ledger_path = tmp_path / "ledger.jsonl"
        bullet = SalienceBullet(
            text="watch for X", source_lesson_ulid="L1", review_after="2026-01-01"
        )
        append_ledger_entries(ledger_path, [ledger_entry_for(bullet, "critic")])
        retire_ledger_entries(
            ledger_path, role="critic", texts=["watch for X"], reason="explicit_removal"
        )
        # Still "committed" in this test's roles.toml stand-in, but its
        # latest ledger status is now retired, not active.
        definition = _role(project_salience=["watch for X"])
        from datetime import date

        due = find_review_due_bullets(definition, ledger_path, today=date(2026, 6, 30))
        assert due == []


class TestBuildRetirementPatch:
    def test_removes_matching_bullet(self):
        definition = _role(project_salience=["keep this", "retire this"])
        patch = build_retirement_patch(definition, ["retire this"])
        assert [b.text for b in patch.accepted] == ["keep this"]
        assert len(patch.dropped) == 1
        assert patch.dropped[0].text == "retire this"
        assert patch.dropped[0].reason == "retired: explicit"
        assert patch.has_changes is True

    def test_normalization_insensitive_match(self):
        definition = _role(project_salience=["watch for X"])
        patch = build_retirement_patch(definition, ["  watch   for   X  "])
        assert patch.accepted == ()
        assert len(patch.dropped) == 1

    def test_unmatched_retire_target_surfaced_not_silent(self):
        """A retire_texts entry matching nothing must be visible in
        dropped, not vanish silently (no-silent-truncation principle)."""
        definition = _role(project_salience=["keep this"])
        patch = build_retirement_patch(definition, ["not present"])
        assert [b.text for b in patch.accepted] == ["keep this"]
        assert len(patch.dropped) == 1
        assert patch.dropped[0].text == "not present"
        assert patch.dropped[0].reason == "retire_target_not_found"
        assert patch.has_changes is False  # nothing was actually retired

    def test_mixed_matched_and_unmatched_retire_targets(self):
        definition = _role(project_salience=["keep this", "retire this"])
        patch = build_retirement_patch(definition, ["retire this", "typo'd bullet"])
        assert [b.text for b in patch.accepted] == ["keep this"]
        reasons = {d.text: d.reason for d in patch.dropped}
        assert reasons["retire this"] == "retired: explicit"
        assert reasons["typo'd bullet"] == "retire_target_not_found"

    def test_empty_retire_list_keeps_all(self):
        definition = _role(project_salience=["a", "b"])
        patch = build_retirement_patch(definition, [])
        assert [b.text for b in patch.accepted] == ["a", "b"]
        assert patch.dropped == ()
        assert patch.has_changes is False

    def test_retirement_patch_applies_via_apply_salience_patch(self):
        import tomlkit

        definition = _role(project_salience=["keep this", "retire this"])
        patch = build_retirement_patch(definition, ["retire this"])
        doc = tomlkit.parse(
            "[roles.critic]\n"
            'description = "d"\n'
            'canonical_role = "critic"\n'
            "produces = []\n"
            'boundary = "b"\n'
            "handoff_to = []\n"
            'instructions = "i"\n'
            'entry_style = "e"\n'
            'when_to_use = "w"\n'
            'collaborate_with = "c"\n'
            'project_salience = ["keep this", "retire this"]\n'
        )
        apply_salience_patch(
            project_toml_doc=doc, patch=patch, bundled_definition=definition
        )
        rendered = tomlkit.dumps(doc)
        assert "keep this" in rendered
        assert "retire this" not in rendered


class TestRetireLedgerEntries:
    def test_appends_retired_status(self, tmp_path):
        ledger_path = tmp_path / "ledger.jsonl"
        bullet = SalienceBullet(text="watch for X", source_lesson_ulid="L1")
        append_ledger_entries(ledger_path, [ledger_entry_for(bullet, "critic")])

        new_entries = retire_ledger_entries(
            ledger_path,
            role="critic",
            texts=["watch for X"],
            reason="explicit_removal",
            projected_at=2000.0,
        )
        assert len(new_entries) == 1
        assert new_entries[0].status == "retired"
        assert new_entries[0].source_lesson_ulid == "L1"  # carried forward

        all_entries = load_ledger_entries(ledger_path)
        assert len(all_entries) == 2  # original active + new retired

    def test_superseded_reason_writes_superseded_status(self, tmp_path):
        ledger_path = tmp_path / "ledger.jsonl"
        new_entries = retire_ledger_entries(
            ledger_path, role="critic", texts=["watch for X"], reason="superseded"
        )
        assert new_entries[0].status == "superseded"

    def test_retirement_makes_bullet_unledgered_afterward(self, tmp_path):
        ledger_path = tmp_path / "ledger.jsonl"
        bullet = SalienceBullet(text="watch for X", source_lesson_ulid="L1")
        append_ledger_entries(ledger_path, [ledger_entry_for(bullet, "critic")])
        retire_ledger_entries(
            ledger_path, role="critic", texts=["watch for X"], reason="explicit_removal"
        )
        definition = _role(project_salience=["watch for X"])
        report = verify_ledger_provenance(definition, ledger_path)
        assert report.unledgered == ("watch for X",)

    def test_no_prior_entry_still_writes_record(self, tmp_path):
        """Retiring a bullet with no prior ledger record (e.g. a
        hand-authored bullet) still writes a retirement record — the ledger
        just has no source_lesson_ulid to carry forward. authority_basis
        must NOT fabricate "human_promoted_lesson" for a bullet that was
        never actually ledgered."""
        ledger_path = tmp_path / "ledger.jsonl"
        new_entries = retire_ledger_entries(
            ledger_path, role="critic", texts=["hand bullet"], reason="explicit_removal"
        )
        assert new_entries[0].source_lesson_ulid == ""
        assert new_entries[0].authority_basis == "unledgered_at_retirement"

    def test_prior_entry_authority_basis_carried_forward(self, tmp_path):
        ledger_path = tmp_path / "ledger.jsonl"
        bullet = SalienceBullet(text="watch for X", source_lesson_ulid="L1")
        append_ledger_entries(ledger_path, [ledger_entry_for(bullet, "critic")])
        new_entries = retire_ledger_entries(
            ledger_path, role="critic", texts=["watch for X"], reason="explicit_removal"
        )
        assert new_entries[0].authority_basis == "human_promoted_lesson"
        assert new_entries[0].status == "retired"


class TestRetirementEndToEnd:
    """Phase 4 acceptance: a bullet leaves roles.toml via each path; ledger
    marks retired/superseded; stale bullets surface for review."""

    def test_review_after_retirement_flow(self, tmp_path):
        from datetime import date

        ledger_path = tmp_path / "ledger.jsonl"
        bullet = SalienceBullet(
            text="stale bullet", source_lesson_ulid="L1", review_after="2026-01-01"
        )
        append_ledger_entries(ledger_path, [ledger_entry_for(bullet, "critic")])
        definition = _role(project_salience=["stale bullet", "fresh bullet"])

        due = find_review_due_bullets(definition, ledger_path, today=date(2026, 6, 30))
        assert [b.text for b in due] == ["stale bullet"]

        # Human confirms retirement (L2 review) — proposed via the compiler.
        patch = build_retirement_patch(definition, [b.text for b in due])
        assert [b.text for b in patch.accepted] == ["fresh bullet"]

        retire_ledger_entries(
            ledger_path,
            role="critic",
            texts=[b.text for b in due],
            reason="review_after_passed",
        )
        # No salience bullet becomes permanent: re-scanning finds nothing
        # due (it's retired, not active) and it's gone from the definition
        # a human would land next.
        remaining_definition = _role(project_salience=["fresh bullet"])
        assert find_review_due_bullets(remaining_definition, ledger_path) == []
