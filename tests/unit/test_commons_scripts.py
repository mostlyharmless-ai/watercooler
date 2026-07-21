"""Commons Phase-0 shared scripts (review #1132 findings coverage).

- candidates: append-only lifecycle resolution (promoted/rejected/expired are
  NOT pending), fail-loud sidecar loading.
- export: governance gate (approval must be a human-authorized Decision;
  evidence must resolve), version bumps stale prior projections atomically,
  idempotent re-runs.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from ulid import ULID

from watercooler.commands_graph import append_entry

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "scripts" / f"{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


candidates_mod = _load("commons_candidates")
export_mod = _load("commons_export")

TOPIC = "topic-x"


def _candidate_body() -> str:
    return (
        "Spec: learnings\n"
        "Candidate-Type: Learning\n"
        "Candidate-Status: needs_human_confirmation\n"
        "Surface-Kind: learning\n"
        "Authority: none\n"
        "Confidence: 4/5\n\n"
        "## Candidate learning\nAlways verify before asserting.\n\n"
        "## Root cause\nSpeculation presented as observation.\n\n"
        "## Fix\nCite the determining evidence.\n\n"
        "## Evidence (verbatim)\n> quoted line\n"
    )


@pytest.fixture
def threads_dir(tmp_path):
    d = tmp_path / ".watercooler"
    d.mkdir()
    return d


def _seed_candidate(threads_dir, *, dispose: str | None = None) -> str:
    cand_id = str(ULID())
    append_entry(
        TOPIC, threads_dir=threads_dir, agent="Daemon", role="scribe",
        title="Learning candidate", entry_type="Note", body=_candidate_body(),
        ball="Caleb", status="OPEN", entry_id=cand_id,
    )
    if dispose:
        append_entry(
            TOPIC, threads_dir=threads_dir, agent="Caleb", role="pm",
            title=f"CandidateDisposition: {dispose}", entry_type="Note",
            body=(
                "Spec: candidate-disposition\n"
                f"CandidateDisposition: {dispose}\n"
                f"Disposition-Target: {cand_id}\n"
            ),
            entry_id=str(ULID()),
        )
    return cand_id


class TestCandidatesLifecycle:
    def _pending(self, threads_dir):
        from datetime import datetime, timezone

        evidence = candidates_mod.gather(
            threads_dir, datetime(2020, 1, 1, tzinfo=timezone.utc), {}
        )
        return evidence["pending"]

    def test_open_candidate_counts_as_pending(self, threads_dir):
        _seed_candidate(threads_dir)
        assert len(self._pending(threads_dir)) == 1

    @pytest.mark.parametrize("kind", ["promoted", "rejected", "expired"])
    def test_resolved_candidates_are_not_pending(self, threads_dir, kind):
        # Review #1132 P1: Candidate-Status is append-only — pending-ness is
        # the resolved lifecycle state, not the original marker.
        _seed_candidate(threads_dir, dispose=kind)
        assert self._pending(threads_dir) == []

    def test_promoted_entry_alone_resolves(self, threads_dir):
        cand_id = _seed_candidate(threads_dir)
        append_entry(
            TOPIC, threads_dir=threads_dir, agent="Caleb", role="pm",
            title="Lesson", entry_type="Note",
            body=(
                "Spec: learnings-promoted\n"
                f"Promoted-From: {cand_id}\n"
                "Authority-Basis: human_promoted\n\n"
                "## Lesson\nAlways verify.\n"
            ),
            entry_id=str(ULID()),
        )
        assert self._pending(threads_dir) == []


class TestSidecarFailLoud:
    def test_missing_sidecar_aborts_with_repair_message(self, tmp_path):
        with pytest.raises(SystemExit, match="not found"):
            candidates_mod.load_sidecar_slugs(tmp_path / "nope.json")

    def test_invalid_json_aborts(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        with pytest.raises(SystemExit, match="malformed"):
            candidates_mod.load_sidecar_slugs(p)

    def test_invalid_rows_abort(self, tmp_path):
        p = tmp_path / "rows.json"
        p.write_text(json.dumps({"candidates": [{"wrong": "shape"}]}), encoding="utf-8")
        with pytest.raises(SystemExit, match="malformed"):
            candidates_mod.load_sidecar_slugs(p)

    def test_valid_sidecar_loads_bare_slugs(self, tmp_path):
        p = tmp_path / "ok.json"
        p.write_text(
            json.dumps({"candidates": [
                {"candidate_ulid": "01A", "root_cause_canonical": "silent-failure@1"}
            ]}),
            encoding="utf-8",
        )
        assert candidates_mod.load_sidecar_slugs(p) == {"01A": "silent-failure"}


# ---------------------------------------------------------------------------
# Export — governance gate + version staling
# ---------------------------------------------------------------------------

BLOCK = (
    "# Doc\n\n"
    "<!-- generated-block:project-conventions -->\n"
    "<!-- /generated-block:project-conventions -->\n"
)


@pytest.fixture
def export_root(tmp_path):
    (tmp_path / "CLAUDE.md").write_text(BLOCK, encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text(BLOCK, encoding="utf-8")
    return tmp_path


# A syntactically valid ULID (Crockford base32 — no I, L, O, U).
CRIT = "01CRTERN0000000000000000AA"


def _seed_decision(
    threads_dir,
    *,
    versions: tuple[int, ...] = (1,),
    bind_criterion: bool = True,
    authority_fields: dict | None = None,
    body_authorization: str = "",
) -> tuple[str, str, str, str]:
    """Seed evidence + an approval Decision BOUND to CRIT; return their refs.

    The Decision carries TRUSTED GRAPH authority fields (the L3 write path's
    ``human_authorized_by`` + ``authority_basis`` node fields — pass
    ``authority_fields={}`` to omit them) plus Criterion-ID /
    Criterion-Version body markers and cites the evidence ULID (the rereview
    binding contract). ``versions`` lists every version this Decision
    approves; ``body_authorization`` injects agent-authorable body text.
    """
    dec_id, ev_id = str(ULID()), str(ULID())
    append_entry(
        TOPIC, threads_dir=threads_dir, agent="Caleb", role="pm",
        title="evidence", entry_type="Note", body="## Lesson\nevidence text\n",
        ball="Caleb", status="OPEN", entry_id=ev_id,
    )
    binding = ""
    if bind_criterion:
        binding = f"Criterion-ID: {CRIT}\n" + "".join(
            f"Criterion-Version: {v}\n" for v in versions
        )
    if authority_fields is None:
        authority_fields = {
            "human_authorized_by": "github:caleb",
            "authority_basis": "human_endorsed",
        }
    append_entry(
        TOPIC, threads_dir=threads_dir, agent="Caleb", role="pm",
        title="Decision — approve criterion", entry_type="Decision",
        body=(
            f"Spec: pm\n{body_authorization}\n{binding}\n"
            f"## Decision\nApprove the criterion, resting on evidence {ev_id}.\n"
        ),
        entry_id=dec_id,
        authority_fields=authority_fields or None,
    )
    # Indices: evidence=0, decision=1.
    return f"{TOPIC}:1 ({dec_id})", f"{TOPIC}:0 ({ev_id})", dec_id, ev_id


def _run_export(threads_dir, export_root, *, version=1, extra=None):
    argv = [
        "--criterion-id", CRIT, "--version", str(version),
        "--statement", f"Test criterion v{version}",
        "--approval-ref", _run_export.approval,
        "--evidence-ref", _run_export.evidence,
        "--threads-dir", str(threads_dir),
        "--repo-root", str(export_root),
    ] + (extra or [])
    return export_mod.main(argv)


class TestExportGovernanceGate:
    def test_bogus_refs_are_refused_even_dry_run(self, threads_dir, export_root):
        with pytest.raises(SystemExit, match="canonical"):
            export_mod.main([
                "--criterion-id", CRIT, "--statement", "x",
                "--approval-ref", "not-a-decision",
                "--evidence-ref", "not-evidence",
                "--threads-dir", str(threads_dir),
                "--repo-root", str(export_root), "--dry-run",
            ])

    def test_unresolvable_approval_is_refused(self, threads_dir, export_root):
        ghost = str(ULID())
        with pytest.raises(SystemExit, match="does not resolve"):
            export_mod.main([
                "--criterion-id", CRIT, "--statement", "x",
                "--approval-ref", f"{TOPIC}:9 ({ghost})",
                "--evidence-ref", f"{TOPIC}:8 ({ghost})",
                "--threads-dir", str(threads_dir),
                "--repo-root", str(export_root), "--dry-run",
            ])

    def test_non_decision_approval_is_refused(self, threads_dir, export_root):
        approval, evidence, _, ev_id = _seed_decision(threads_dir)
        with pytest.raises(SystemExit, match="not a Decision"):
            export_mod.main([
                "--criterion-id", CRIT, "--statement", "x",
                "--approval-ref", evidence,  # a Note, not a Decision
                "--evidence-ref", evidence,
                "--threads-dir", str(threads_dir),
                "--repo-root", str(export_root), "--dry-run",
            ])

    def test_verified_refs_pass_and_export_writes(self, threads_dir, export_root):
        approval, evidence, _, _ = _seed_decision(threads_dir)
        _run_export.approval, _run_export.evidence = approval, evidence
        assert _run_export(threads_dir, export_root) == 0
        for name in ("CLAUDE.md", "AGENTS.md"):
            text = (export_root / name).read_text(encoding="utf-8")
            assert f"derived-from {CRIT}@1" in text
        ledger = export_root / "dev_docs" / "research" / "commons-export-ledger.jsonl"
        assert len(ledger.read_text().strip().splitlines()) == 1


class TestExportBindingGate:
    """Rereview #1132 P1: trusted graph authority, bound to the subject."""

    def test_negated_prose_authorization_is_refused(self, threads_dir, export_root):
        # "not authorized by" contains the old prose needle — graph-only now.
        approval, evidence, _, _ = _seed_decision(
            threads_dir,
            authority_fields={},
            body_authorization="This criterion was not authorized by anyone.",
        )
        _run_export.approval, _run_export.evidence = approval, evidence
        with pytest.raises(SystemExit, match="trusted graph authority"):
            _run_export(threads_dir, export_root, extra=["--dry-run"])

    def test_body_marker_only_authorization_is_refused(self, threads_dir, export_root):
        # Rereview round 3: a fully bound Decision whose ONLY authorization is
        # the agent-authorable `Human-Authorized-By:` body marker must fail —
        # ordinary say can write that line; only the gated L3 path stamps the
        # graph fields.
        approval, evidence, _, _ = _seed_decision(
            threads_dir,
            authority_fields={},
            body_authorization="Human-Authorized-By: github:caleb",
        )
        _run_export.approval, _run_export.evidence = approval, evidence
        with pytest.raises(SystemExit, match="trusted graph authority"):
            _run_export(threads_dir, export_root, extra=["--dry-run"])

    def test_wrong_authority_basis_is_refused(self, threads_dir, export_root):
        # human_authorized_by present but basis "none" (the unauthorized L3
        # write shape) does not authorize an export.
        approval, evidence, _, _ = _seed_decision(
            threads_dir,
            authority_fields={
                "human_authorized_by": "github:caleb",
                "authority_basis": "none",
            },
        )
        _run_export.approval, _run_export.evidence = approval, evidence
        with pytest.raises(SystemExit, match="trusted graph authority"):
            _run_export(threads_dir, export_root, extra=["--dry-run"])

    def test_unrelated_authorized_decision_is_refused(self, threads_dir, export_root):
        # Authorized, but carries no Criterion-ID binding to THIS criterion.
        approval, evidence, _, _ = _seed_decision(threads_dir, bind_criterion=False)
        _run_export.approval, _run_export.evidence = approval, evidence
        with pytest.raises(SystemExit, match="Criterion-ID"):
            _run_export(threads_dir, export_root, extra=["--dry-run"])

    def test_version_unbound_decision_is_refused(self, threads_dir, export_root):
        approval, evidence, _, _ = _seed_decision(threads_dir, versions=(1,))
        _run_export.approval, _run_export.evidence = approval, evidence
        with pytest.raises(SystemExit, match="Criterion-Version"):
            _run_export(threads_dir, export_root, version=2, extra=["--dry-run"])

    def test_wrong_index_in_ref_is_refused(self, threads_dir, export_root):
        approval, evidence, dec_id, _ = _seed_decision(threads_dir)
        # Real ULID, wrong canonical position (decision sits at index 1, not 0).
        _run_export.approval = f"{TOPIC}:0 ({dec_id})"
        _run_export.evidence = evidence
        with pytest.raises(SystemExit, match="index mismatch"):
            _run_export(threads_dir, export_root, extra=["--dry-run"])

    def test_uncited_evidence_is_refused(self, threads_dir, export_root):
        approval, _, _, _ = _seed_decision(threads_dir)
        # Seed a second, real evidence Note the Decision does NOT cite.
        stray_id = str(ULID())
        append_entry(
            TOPIC, threads_dir=threads_dir, agent="Caleb", role="pm",
            title="stray evidence", entry_type="Note", body="## Lesson\nstray\n",
            entry_id=stray_id,
        )
        _run_export.approval = approval
        _run_export.evidence = f"{TOPIC}:2 ({stray_id})"
        with pytest.raises(SystemExit, match="does not reference"):
            _run_export(threads_dir, export_root, extra=["--dry-run"])


class TestExportVersioning:
    def test_version_bump_stales_prior_in_both_targets(self, threads_dir, export_root):
        approval, evidence, _, _ = _seed_decision(threads_dir, versions=(1, 2))
        _run_export.approval, _run_export.evidence = approval, evidence
        assert _run_export(threads_dir, export_root, version=1) == 0
        assert _run_export(threads_dir, export_root, version=2) == 0
        for name in ("CLAUDE.md", "AGENTS.md"):
            text = (export_root / name).read_text(encoding="utf-8")
            # @2 active; @1 flagged stale in both marker and prose.
            assert f"derived-from {CRIT}@2" in text
            assert f"STALE derived-from {CRIT}@1 | superseded-by @2" in text
            assert "[STALE — superseded by @2]" in text
            # exactly one ACTIVE (non-stale) marker for this criterion
            active = [
                line for line in text.splitlines()
                if f"derived-from {CRIT}@" in line and "STALE" not in line
            ]
            assert len(active) == 1

    def test_rerun_same_version_is_idempotent(self, threads_dir, export_root):
        approval, evidence, _, _ = _seed_decision(threads_dir)
        _run_export.approval, _run_export.evidence = approval, evidence
        assert _run_export(threads_dir, export_root, version=1) == 0
        assert _run_export(threads_dir, export_root, version=1) == 0
        for name in ("CLAUDE.md", "AGENTS.md"):
            text = (export_root / name).read_text(encoding="utf-8")
            assert text.count(f"derived-from {CRIT}@1") == 1
        ledger = export_root / "dev_docs" / "research" / "commons-export-ledger.jsonl"
        assert len(ledger.read_text().strip().splitlines()) == 1  # no double-count

    def test_equivalent_statement_format_rerun_is_idempotent(
        self, threads_dir, export_root
    ):
        # Rereview #1132 P2 (round 6): the bullet canonicalizes terminal
        # punctuation but the ledger stored the raw stripped input, so
        # "Keep this safe" then "Keep this safe." produced byte-identical
        # bullets yet a ledger identity mismatch. One canonical statement
        # representation everywhere: the rerun is an idempotent success.
        approval, evidence, _, _ = _seed_decision(threads_dir)
        _run_export.approval, _run_export.evidence = approval, evidence
        assert _run_export(
            threads_dir, export_root, extra=["--statement", "Keep this safe"]
        ) == 0
        assert _run_export(
            threads_dir, export_root, extra=["--statement", "Keep this safe."]
        ) == 0
        ledger = export_root / "dev_docs" / "research" / "commons-export-ledger.jsonl"
        records = [json.loads(x) for x in ledger.read_text().strip().splitlines()]
        assert len(records) == 1
        assert records[0]["statement"] == "Keep this safe."
        for name in ("CLAUDE.md", "AGENTS.md"):
            text = (export_root / name).read_text(encoding="utf-8")
            assert text.count(f"derived-from {CRIT}@1") == 1

    def test_embedded_whitespace_rerun_is_idempotent(self, threads_dir, export_root):
        # Rereview #1132 P2 (round 7): textwrap.fill collapses embedded
        # newlines/tabs/runs while the ledger preserved them — equivalent
        # inputs rendered byte-identical bullets yet mismatched ledger
        # identities. canonical_statement now collapses whitespace too.
        approval, evidence, _, _ = _seed_decision(threads_dir)
        _run_export.approval, _run_export.evidence = approval, evidence
        assert _run_export(
            threads_dir, export_root, extra=["--statement", "Keep\nthis safe"]
        ) == 0
        assert _run_export(
            threads_dir, export_root,
            extra=["--statement", "Keep \t this   safe."],
        ) == 0
        ledger = export_root / "dev_docs" / "research" / "commons-export-ledger.jsonl"
        records = [json.loads(x) for x in ledger.read_text().strip().splitlines()]
        assert len(records) == 1
        assert records[0]["statement"] == "Keep this safe."
        for name in ("CLAUDE.md", "AGENTS.md"):
            text = (export_root / name).read_text(encoding="utf-8")
            assert text.count(f"derived-from {CRIT}@1") == 1

    def test_version_downgrade_is_refused(self, threads_dir, export_root):
        # Rereview #1132 P1: after @2 is live, re-exporting @1 must be rejected
        # (it would restore two active projections of one criterion).
        approval, evidence, _, _ = _seed_decision(threads_dir, versions=(1, 2))
        _run_export.approval, _run_export.evidence = approval, evidence
        assert _run_export(threads_dir, export_root, version=2) == 0
        with pytest.raises(SystemExit, match="forward-only"):
            _run_export(threads_dir, export_root, version=1)

    def test_missing_ledger_record_is_reconciled_on_retry(self, threads_dir, export_root):
        # Rereview #1132 P2: both targets written + ledger append lost → the
        # retry must repair the provenance record, not exit before writing it.
        approval, evidence, _, _ = _seed_decision(threads_dir)
        _run_export.approval, _run_export.evidence = approval, evidence
        assert _run_export(threads_dir, export_root) == 0
        ledger = export_root / "dev_docs" / "research" / "commons-export-ledger.jsonl"
        ledger.unlink()  # simulate the append failing after both target writes
        assert _run_export(threads_dir, export_root) == 0
        records = [json.loads(x) for x in ledger.read_text().strip().splitlines()]
        assert len(records) == 1
        assert records[0]["candidate_id"] == CRIT
        assert records[0]["version"] == 1
        assert records[0]["reconciled"] is True
        # And a further retry with the record present appends nothing.
        assert _run_export(threads_dir, export_root) == 0
        assert len(ledger.read_text().strip().splitlines()) == 1


class TestSameVersionContentVerification:
    """Rereview #1132 P1 (round 3): `present` must be content-verified.

    A same-version re-run whose statement, approval, or evidence differs from
    the on-disk projection is a supersession without a version bump — refused,
    never silently "success", and never fresh ledger provenance for a stale
    on-disk bullet.
    """

    def test_changed_statement_same_version_is_refused(self, threads_dir, export_root):
        approval, evidence, _, _ = _seed_decision(threads_dir)
        _run_export.approval, _run_export.evidence = approval, evidence
        assert _run_export(threads_dir, export_root) == 0
        with pytest.raises(SystemExit, match="Same-version rewrites are refused"):
            _run_export(
                threads_dir, export_root, extra=["--statement", "A different rule"]
            )

    def test_changed_approval_same_version_is_refused(self, threads_dir, export_root):
        approval, evidence, _, ev_id = _seed_decision(threads_dir)
        _run_export.approval, _run_export.evidence = approval, evidence
        assert _run_export(threads_dir, export_root) == 0
        # A second, equally valid approval Decision citing the SAME evidence
        # (lands at index 2).
        dec2 = str(ULID())
        append_entry(
            TOPIC, threads_dir=threads_dir, agent="Caleb", role="pm",
            title="Decision — approve criterion again", entry_type="Decision",
            body=(
                f"Spec: pm\nCriterion-ID: {CRIT}\nCriterion-Version: 1\n\n"
                f"## Decision\nApprove again, resting on evidence {ev_id}.\n"
            ),
            entry_id=dec2,
            authority_fields={
                "human_authorized_by": "github:caleb",
                "authority_basis": "human_endorsed",
            },
        )
        _run_export.approval = f"{TOPIC}:2 ({dec2})"
        with pytest.raises(SystemExit, match="Same-version rewrites are refused"):
            _run_export(threads_dir, export_root)

    def test_changed_evidence_same_version_is_refused(self, threads_dir, export_root):
        approval, evidence, _, ev_id = _seed_decision(threads_dir)
        _run_export.approval, _run_export.evidence = approval, evidence
        assert _run_export(threads_dir, export_root) == 0
        # Same evidence entry, cited twice — a different evidence SET at the
        # same version changes the provenance identity.
        with pytest.raises(SystemExit, match="Same-version rewrites are refused"):
            export_mod.main([
                "--criterion-id", CRIT, "--version", "1",
                "--statement", "Test criterion v1",
                "--approval-ref", approval,
                "--evidence-ref", evidence, "--evidence-ref", evidence,
                "--threads-dir", str(threads_dir),
                "--repo-root", str(export_root),
            ])

    def test_missing_ledger_plus_changed_content_is_refused(
        self, threads_dir, export_root
    ):
        # The P2 reconcile path must never append fresh provenance for a
        # DIFFERENT on-disk projection: content mismatch refuses before the
        # ledger is touched.
        approval, evidence, _, _ = _seed_decision(threads_dir)
        _run_export.approval, _run_export.evidence = approval, evidence
        assert _run_export(threads_dir, export_root) == 0
        ledger = export_root / "dev_docs" / "research" / "commons-export-ledger.jsonl"
        ledger.unlink()
        with pytest.raises(SystemExit, match="Same-version rewrites are refused"):
            _run_export(
                threads_dir, export_root, extra=["--statement", "A different rule"]
            )
        assert not ledger.exists()

    def test_mixed_state_refusal_writes_neither_target(self, threads_dir, export_root):
        # Rereview #1132 P1 (round 4): AGENTS.md carries a CONFLICTING @1
        # projection while CLAUDE.md has none. The export must refuse before
        # modifying EITHER file — previously CLAUDE.md was written first and
        # the refusal on AGENTS.md left divergent harness guidance.
        approval, evidence, _, _ = _seed_decision(threads_dir)
        _run_export.approval, _run_export.evidence = approval, evidence
        conflicting = export_mod.build_bullet(
            "An older, different rule", CRIT, 1, approval, [evidence]
        )
        agents = export_root / "AGENTS.md"
        agents.write_text(
            agents.read_text(encoding="utf-8").replace(
                export_mod.BLOCK_CLOSE,
                f"{conflicting}\n\n{export_mod.BLOCK_CLOSE}",
                1,
            ),
            encoding="utf-8",
        )
        before = {
            name: (export_root / name).read_text(encoding="utf-8")
            for name in ("CLAUDE.md", "AGENTS.md")
        }
        with pytest.raises(SystemExit, match="Same-version rewrites are refused"):
            _run_export(threads_dir, export_root)
        for name, text in before.items():
            assert (export_root / name).read_text(encoding="utf-8") == text
        assert not (
            export_root / "dev_docs" / "research" / "commons-export-ledger.jsonl"
        ).exists()

    def test_tampered_ledger_record_is_refused_not_reconciled(
        self, threads_dir, export_root
    ):
        # Rereview #1132 P2 (round 4): a ledger record matched on
        # criterion/version alone let a tampered statement/hash pass as
        # existing provenance. Identity now includes content_sha256; a
        # mismatching record refuses instead of reporting idempotent success.
        approval, evidence, _, _ = _seed_decision(threads_dir)
        _run_export.approval, _run_export.evidence = approval, evidence
        assert _run_export(threads_dir, export_root) == 0
        ledger = export_root / "dev_docs" / "research" / "commons-export-ledger.jsonl"
        record = json.loads(ledger.read_text(encoding="utf-8").strip())
        record["statement"] = "A falsified statement"
        record["content_sha256"] = "0" * 64
        ledger.write_text(json.dumps(record) + "\n", encoding="utf-8")
        with pytest.raises(SystemExit, match="ledger provenance conflict"):
            _run_export(threads_dir, export_root)
        # The false record is preserved for investigation, never overwritten.
        assert json.loads(ledger.read_text(encoding="utf-8").strip()) == record

    def test_falsified_statement_with_original_hash_is_refused(
        self, threads_dir, export_root
    ):
        # Rereview #1132 P2 (round 5): a record whose stored hash still
        # matches but whose STATEMENT was falsified passed as existing
        # provenance. The full record identity (statement, approval,
        # evidence, hash) must be internally consistent.
        approval, evidence, _, _ = _seed_decision(threads_dir)
        _run_export.approval, _run_export.evidence = approval, evidence
        assert _run_export(threads_dir, export_root) == 0
        ledger = export_root / "dev_docs" / "research" / "commons-export-ledger.jsonl"
        record = json.loads(ledger.read_text(encoding="utf-8").strip())
        record["statement"] = "A falsified statement"  # hash left intact
        ledger.write_text(json.dumps(record) + "\n", encoding="utf-8")
        with pytest.raises(SystemExit, match="ledger provenance conflict"):
            _run_export(threads_dir, export_root)
        assert json.loads(ledger.read_text(encoding="utf-8").strip()) == record

    def test_matching_row_cannot_hide_conflicting_duplicate(
        self, threads_dir, export_root
    ):
        # Rereview #1132 P2 (round 5): returning on the first matching row
        # let a good record mask a conflicting duplicate at the same
        # (criterion, version). EVERY same-version record must carry the
        # export identity.
        approval, evidence, _, _ = _seed_decision(threads_dir)
        _run_export.approval, _run_export.evidence = approval, evidence
        assert _run_export(threads_dir, export_root) == 0
        ledger = export_root / "dev_docs" / "research" / "commons-export-ledger.jsonl"
        good = json.loads(ledger.read_text(encoding="utf-8").strip())
        bad = dict(good, statement="A conflicting duplicate", content_sha256="0" * 64)
        ledger.write_text(
            json.dumps(good) + "\n" + json.dumps(bad) + "\n", encoding="utf-8"
        )
        with pytest.raises(SystemExit, match="ledger provenance conflict"):
            _run_export(threads_dir, export_root)

    def test_conflicting_record_with_empty_targets_refuses_before_writes(
        self, threads_dir, export_root
    ):
        # Rereview #1132 P2 (round 5): the conflict check ran only on the
        # all-present path — empty targets plus a pre-existing wrong-identity
        # record wrote both files and appended a second record. It must now
        # refuse BEFORE any target or ledger write.
        approval, evidence, _, _ = _seed_decision(threads_dir)
        _run_export.approval, _run_export.evidence = approval, evidence
        ledger = export_root / "dev_docs" / "research" / "commons-export-ledger.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        stale_record = {
            "candidate_id": CRIT,
            "version": 1,
            "statement": "Some other statement",
            "evidence_refs": [evidence],
            "approval_decision": approval,
            "content_sha256": "f" * 64,
        }
        ledger.write_text(json.dumps(stale_record) + "\n", encoding="utf-8")
        before = {
            name: (export_root / name).read_text(encoding="utf-8")
            for name in ("CLAUDE.md", "AGENTS.md")
        }
        with pytest.raises(SystemExit, match="ledger provenance conflict"):
            _run_export(threads_dir, export_root)
        for name, text in before.items():
            assert (export_root / name).read_text(encoding="utf-8") == text
        assert (
            len(ledger.read_text(encoding="utf-8").strip().splitlines()) == 1
        )

    def test_matching_record_with_reverted_targets_restores_without_duplicate(
        self, threads_dir, export_root
    ):
        # Complement to the conflict cases: a MATCHING record with reverted
        # target files restores the projections without appending a
        # duplicate provenance row.
        approval, evidence, _, _ = _seed_decision(threads_dir)
        _run_export.approval, _run_export.evidence = approval, evidence
        assert _run_export(threads_dir, export_root) == 0
        for name in ("CLAUDE.md", "AGENTS.md"):
            (export_root / name).write_text(BLOCK, encoding="utf-8")  # revert
        ledger = export_root / "dev_docs" / "research" / "commons-export-ledger.jsonl"
        assert _run_export(threads_dir, export_root) == 0
        for name in ("CLAUDE.md", "AGENTS.md"):
            text = (export_root / name).read_text(encoding="utf-8")
            assert f"derived-from {CRIT}@1" in text
        assert len(ledger.read_text(encoding="utf-8").strip().splitlines()) == 1
