"""F3 — versioned canonical root-cause taxonomy (Commons cluster Phase 2).

Decision 01KXQ32Q7Z41F0P7A1JHN0S527 / plan workflow-packs…:85: recurrence over
raw LLM root-cause strings is noise, so every durable surface stamps
`Root-Cause-Canonical: <slug>@<taxonomy-version>`; promotion recomputes the slug
from the EFFECTIVE (post-edits) root cause; the historical corpus is backfilled
via a frozen, deterministic sidecar.
"""

from __future__ import annotations

import json
from pathlib import Path

from watercooler.learning_extraction import (
    canonical_root_cause_stamp,
    load_root_cause_taxonomy,
    normalize_root_cause,
    root_cause_taxonomy_version,
)
from watercooler.learning_synthesis import (
    LearningDraft,
    SynthesisResult,
    format_learning_candidate_body,
)
from watercooler.promotion import format_promotion_lesson_body, parse_candidate_body

CAND = "01AAAAAAAAAAAAAAAAAAAAAAAA"

_SIDECAR = (
    Path(__file__).resolve().parents[2]
    / "dev_docs"
    / "research"
    / "2026-07-17-root-cause-sidecar-v1.json"
)


class TestTaxonomyResource:
    def test_loads_via_packaged_resource_with_int_version(self):
        data = load_root_cause_taxonomy()
        assert isinstance(data["version"], int)
        assert data["category"], "taxonomy must have categories"

    def test_other_is_the_final_fail_open_category(self):
        cats = load_root_cause_taxonomy()["category"]
        assert cats[-1]["slug"] == "other"
        assert cats[-1]["hints"] == []


class TestNormalizer:
    def test_mapping_table(self):
        assert (
            normalize_root_cause(
                "Async worker pushed commits between attempts causing rebase divergence"
            )
            == "sync-concurrency"
        )
        assert normalize_root_cause("PushError swallowed by caller") == "silent-failure"
        assert (
            normalize_root_cause(
                "Fragmentation of configuration files across multiple mechanisms"
            )
            == "config-fragmentation"
        )
        assert (
            normalize_root_cause("Leaked secrets in git and header-trust auth")
            == "security-exposure"
        )

    def test_unknown_and_empty_fall_open_to_other(self):
        assert normalize_root_cause("a completely novel phenomenon") == "other"
        assert normalize_root_cause("") == "other"
        assert normalize_root_cause(None) == "other"

    def test_stamp_is_version_bound(self):
        v = root_cause_taxonomy_version()
        assert canonical_root_cause_stamp("PushError swallowed") == f"silent-failure@{v}"


def _draft(root_cause: str) -> SynthesisResult:
    return SynthesisResult(
        topic="t",
        passed=True,
        confidence=4,
        draft=LearningDraft(
            root_cause=root_cause,
            lesson="Verify before asserting.",
            problem_summary="p",
            fix_summary="cite evidence",
            confidence=4,
            verbatim_quotes=["q"],
        ),
    )


class TestRendererRoundTrip:
    def test_candidate_body_stamp_round_trips(self):
        body = format_learning_candidate_body(
            _draft("PushError swallowed by caller"), topic="t", pr_numbers=[1]
        )
        v = root_cause_taxonomy_version()
        assert f"Root-Cause-Canonical: silent-failure@{v}" in body
        meta = parse_candidate_body(body, CAND, "t")
        assert meta.root_cause_canonical == "silent-failure"
        assert meta.root_cause_taxonomy_version == v

    def test_promoted_body_stamp_round_trips(self):
        cand_body = format_learning_candidate_body(
            _draft("PushError swallowed by caller"), topic="t", pr_numbers=[1]
        )
        meta = parse_candidate_body(cand_body, CAND, "t")
        promoted = format_promotion_lesson_body(meta, human_authorized_by="github:caleb")
        v = root_cause_taxonomy_version()
        assert f"Root-Cause-Canonical: silent-failure@{v}" in promoted
        again = parse_candidate_body(promoted, CAND, "t")
        assert again.root_cause_canonical == "silent-failure"
        assert again.root_cause_taxonomy_version == v

    def test_promotion_recomputes_slug_from_effective_edited_root_cause(self):
        # Candidate slug = silent-failure; the human edit changes the cause to a
        # sync-concurrency phenomenon — the durable lesson must NOT carry the
        # candidate's stale slug.
        cand_body = format_learning_candidate_body(
            _draft("PushError swallowed by caller"), topic="t", pr_numbers=[1]
        )
        meta = parse_candidate_body(cand_body, CAND, "t")
        promoted = format_promotion_lesson_body(
            meta,
            human_authorized_by="github:caleb",
            edits={"root_cause": "Async worker race during rebase"},
        )
        v = root_cause_taxonomy_version()
        assert f"Root-Cause-Canonical: sync-concurrency@{v}" in promoted
        assert "silent-failure@" not in promoted
        assert "Promotion-Edits: root_cause" in promoted


class TestFrozenSidecar:
    def test_sidecar_exists_is_frozen_and_version_bound(self):
        data = json.loads(_SIDECAR.read_text(encoding="utf-8"))
        assert data["frozen"] is True
        assert isinstance(data["taxonomy_version"], int)
        assert len(data["candidates"]) >= 50

    def test_sidecar_keys_are_bare_ulids(self):
        # Promoted-From / xref values carry BARE ULIDs; a sidecar keyed by the
        # graph's "entry:<ULID>" form would never join (caught live: the
        # commons-candidates report grouped every lesson as unclassified).
        data = json.loads(_SIDECAR.read_text(encoding="utf-8"))
        for row in data["candidates"]:
            assert not row["candidate_ulid"].startswith("entry:"), row["candidate_ulid"]
            assert len(row["candidate_ulid"]) == 26, row["candidate_ulid"]

    def test_sidecar_is_self_consistent_with_the_normalizer(self):
        # Determinism guard: every stored slug must equal what the normalizer
        # produces for the stored raw text at the stored taxonomy version. If
        # the taxonomy version has moved past the sidecar's, the sidecar stays
        # FROZEN (a new version gets a NEW sidecar) — the recompute check then
        # only applies while versions match.
        data = json.loads(_SIDECAR.read_text(encoding="utf-8"))
        if data["taxonomy_version"] != root_cause_taxonomy_version():
            return  # frozen against an older taxonomy — nothing to recheck
        for row in data["candidates"]:
            expected = (
                f"{normalize_root_cause(row['root_cause_raw'])}@{data['taxonomy_version']}"
            )
            assert row["root_cause_canonical"] == expected, row["candidate_ulid"]
