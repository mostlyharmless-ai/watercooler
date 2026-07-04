"""Authority-surface falsification battery (#882).

A growing, deterministic battery that tries to make Watercooler present non-authority
as authority. Each case encodes a failure mode from the epistemic-failure review and
asserts the system's *actual* guarantee — including, where relevant, that the system can
only surface a limitation rather than detect a falsehood.

The constructive-dual framing is a bounded, falsifiable hypothesis: a curated,
provenance-bearing graph plus type-appropriate gates can reduce promotion of plausible
outputs into durable team authority. These tests fix the boundary so product claims stay
honest — when a case proves the system can only *surface a limitation* (high-provenance
wrongness, temporal currency), docs must not claim it *detects* that class.

Seeded by Unit 1 (#878) with the UI-authority case; completed by Unit 5 (#882) with the
remaining cases from the plan (`dev_docs/plans/2026-06-04-feat-authority-surface-hardening-plan.md`).
No paid LLM calls: the summarizer is mocked.
"""

import json
from unittest.mock import patch

import pytest

from watercooler.authority_support import (
    RECORD_STATE_ENTRY_TYPES,
    SUBSTANTIVE_TETHERS,
    TETHER_CONTRACT,
    TETHER_INTERPRETIVE,
    TETHER_TEST,
    TETHER_RECORD_STATE,
    TETHER_SOURCE,
    TETHER_UNKNOWN,
    TETHER_USER,
    build_read_model,
    derive_candidate_support,
)
from watercooler.baseline_graph.summarizer import (
    SUMMARY_SCHEMA_VERSION,
    _launders_authority,
    entry_type_counts,
    summarize_thread,
    summary_is_stale,
)
from watercooler.decision_extraction import quotes_reverified_against_source
from watercooler.promotion import (
    PromotionError,
    format_promotion_decision_body,
    parse_candidate_body,
    validate_candidate_for_promotion,
)
from watercooler.promotion_metrics_lib import (
    STATE_MEASURED,
    STATE_NOT_YET_MEASURABLE,
    STATE_UNKNOWN,
    compute_early_supersession_hazard,
    compute_endogenous_reinforcement_rate,
)
from watercooler import belief_candidate as bc
from watercooler.baseline_graph.storage import get_graph_dir, get_thread_graph_dir

_LLM = "watercooler.baseline_graph.summarizer._call_llm"

# Tokens a consumer could mistake for a collapsed truth/strength verdict. The design
# forbids reducing support to one of these — we match them as *substrings* of the read
# model's field names so a renamed-but-equivalent scalar ("support_score",
# "overall_confidence", "world_truth_verdict") is also caught, not just the bare names.
_VERDICT_TOKENS = (
    "truth",
    "correct",
    "verdict",
    "confidence",
    "score",
    "strength",
    "composite",
)
# Temporal-validity tokens the T1 read model must NOT expose: currency is a T2 property.
_CURRENCY_TOKENS = ("valid_at", "invalid_at", "current", "stale", "in_force", "currency")


def _fields_matching(model, tokens) -> set[str]:
    """Top-level read-model field names containing any of ``tokens`` (substring)."""
    return {f for f in vars(model) for tok in tokens if tok in f.lower()}


class TestHighProvenanceWrongnessCase:
    """Project-record support is not world-truth (#882 high-provenance wrongness).

    A candidate can quote a real, record-typed source verbatim and still be
    substantively wrong about the world. The warrant model grants ``source`` +
    ``record_state`` support because the quote ties to the record — it never asserts the
    claim is true. This is the documented *limitation*: with no test, contract, or
    conflicting record to act as an oracle, a well-cited falsehood earns record/source
    support, and no truth/correctness field exists that could be read as "true".
    """

    _SOURCE_BODY = "We will store sessions in PostgreSQL for durability."
    _VERBATIM_QUOTE = "We will store sessions in PostgreSQL for durability."

    def test_verified_quote_to_record_source_grants_support_not_truth(self):
        # The quote is genuinely in the source, so re-validation passes...
        assert quotes_reverified_against_source([self._VERBATIM_QUOTE], self._SOURCE_BODY)

        model = derive_candidate_support(
            source_entry_id="01SOURCE0000000000000000AA",
            verbatim_quotes=[self._VERBATIM_QUOTE],
            quote_verified=True,
            source_entry_type="Decision",
        )

        # ...and support is granted because the quote ties to a record-state source.
        assert model.support_counts.get(TETHER_SOURCE, 0) >= 1
        assert model.support_counts.get(TETHER_RECORD_STATE, 0) >= 1
        # But the model is purely categorical: no field exists that a consumer could
        # read as "this claim is true". Support says record/source-backed, never true.
        assert not _fields_matching(model, _VERDICT_TOKENS)

    def test_no_oracle_means_no_automatic_correction(self):
        # No test, contract, or conflicting record is supplied — the system has no
        # signal that a well-cited claim is wrong. Honest behavior is to report the
        # backing category, not to flag falsehood.
        model = derive_candidate_support(
            source_entry_id="01SOURCE0000000000000000AA",
            verbatim_quotes=[self._VERBATIM_QUOTE],
            quote_verified=True,
            source_entry_type="Decision",
        )
        # Source is the strongest tether present (record_state is also present but
        # ranks below source); the dominant tether reports backing, not correctness.
        assert model.dominant_tether == TETHER_SOURCE
        assert model.thin_support is False  # "record-backed", explicitly NOT "true".
        # The limitation is load-bearing: the model carries no correction/truth signal
        # that could flag the well-cited falsehood. (Mutation-tested: adding a
        # `world_truth_verdict` field must fail this assertion.)
        assert not _fields_matching(model, _VERDICT_TOKENS)


class TestDisagreementCollapseCase:
    """Consensus-language laundering over a Note-only thread must be caught (#882).

    Scope note: this asserts the *consensus/resolution-prose* half of the plan's
    disagreement case. The summarizer guard suppresses decision/outcome language; it has
    no mechanism to detect "majority view kept, minority dropped", so this battery does
    not claim minority-evidence preservation — only that a Note-only window cannot emit
    settled-consensus language.
    """

    def test_consensus_language_over_disagreement_is_scrubbed(self):
        # A Note-only thread of genuine disagreement.
        entries = [
            {"title": "Position A", "body": "We must use Postgres.", "type": "Note"},
            {"title": "Position B", "body": "No, Redis is required.", "type": "Note"},
            {"title": "Rebuttal", "body": "Postgres handles our load.", "type": "Note"},
        ]
        laundering = (
            "The team reached a consensus: the discussion was resolved in favor of "
            "Postgres."
        )
        with patch(_LLM, return_value=laundering):
            summary = summarize_thread(entries, thread_title="Storage debate")

        # The summary may not claim a consensus/resolution a Note-only thread lacks.
        # (The direct-call test below backstops the detector itself against a no-op.)
        assert not _launders_authority(summary, allow_decision=False, allow_outcome=False)
        # The deterministic badge reports the true shape regardless of prose.
        counts = entry_type_counts(entries)
        assert counts["Decision"] == 0
        assert counts["Closure"] == 0

    def test_consensus_assertion_is_flagged_directly(self):
        # "reached a consensus/resolution/conclusion" is outcome-laundering; with no
        # Closure in the window (allow_outcome=False) the guard must fire. This is the
        # non-circular backstop: it fails if the detector is gutted to always-False.
        prose = "After debate the team reached a consensus on the API shape."
        assert _launders_authority(prose, allow_decision=False, allow_outcome=False)


class TestGateBypassUnderLoadCase:
    """The #886 double-promotion guard scans the whole thread regardless of position
    (#886 -> #882).

    The guarantee under test is positional, not throughput-sensitive: a prior promoted
    Decision blocks re-promotion no matter how many entries it is buried under. (The
    guard is a single linear scan with no batching, so this is the honest framing — it
    is TOCTOU-safe only for serialized retries, per ``validate_candidate_for_promotion``.)
    """

    _CANDIDATE_ID = "01CAND0000000000000000000A"

    def _promoted_decision(self) -> dict:
        return {
            "entry_id": "01PRIORDEC000000000000000A",
            "entry_type": "Decision",
            "body": (
                "## Decision\nUse Postgres for sessions.\n\n"
                f"Promoted-From: {self._CANDIDATE_ID}\n"
                "Authority-Basis: human_promoted\n"
            ),
        }

    def _candidate_meta(self):
        body = (
            "Candidate-Type: Decision\n"
            "Candidate-Status: needs_human_confirmation\n\n"
            "## Candidate Decision\nUse Postgres for sessions.\n"
        )
        return parse_candidate_body(body, self._CANDIDATE_ID, "storage")

    def test_second_promotion_refused_when_prior_decision_buried_in_thread(self):
        meta = self._candidate_meta()
        # 200 unrelated entries plus the prior promoted Decision in the middle: the
        # scan must find it regardless of position.
        entries = [
            {"entry_id": f"01N{i:023d}", "entry_type": "Note", "body": f"chatter {i}"}
            for i in range(200)
        ]
        entries.insert(137, self._promoted_decision())

        with pytest.raises(PromotionError, match="already has a promoted entry"):
            validate_candidate_for_promotion(
                meta,
                "Decision",
                "github:alice",
                existing_thread_entries=entries,
            )

    def test_first_promotion_allowed_when_no_prior_decision(self):
        meta = self._candidate_meta()
        entries = [
            {"entry_id": f"01N{i:023d}", "entry_type": "Note", "body": f"chatter {i}"}
            for i in range(200)
        ]
        # No prior promoted Decision and no disposition -> must not raise.
        validate_candidate_for_promotion(
            meta,
            "Decision",
            "github:alice",
            existing_thread_entries=entries,
        )


class TestUIAuthorityCase:
    """Summary wording/ordering must not turn Notes into Decisions (#878 -> #882)."""

    def test_note_only_summary_cannot_present_as_decision(self):
        # A Note-only thread whose LLM tries to emit decision/outcome language.
        entries = [
            {"title": f"Note {i}", "body": f"Open question {i}.", "type": "Note"}
            for i in range(10)
        ]
        laundering = "Key decisions: defer Phase 1a. The outcome is resolved."
        with patch(_LLM, return_value=laundering):
            summary = summarize_thread(entries, thread_title="Discussion")

        # The summary may not assert decisions/outcomes the thread does not contain...
        assert not _launders_authority(summary, allow_decision=False, allow_outcome=False)
        # ...and the deterministic badge reports the true shape regardless of prose.
        counts = entry_type_counts(entries)
        assert counts["Decision"] == 0
        assert counts["Closure"] == 0
        assert counts["Note"] == 10

    def test_badge_is_independent_of_summary_prose(self):
        # Even if a laundered summary survived, the badge is computed from entries, not
        # from prose — so a consumer always has a non-LLM ground truth for thread shape.
        entries = [{"title": "n", "body": "b", "type": "Note"}]
        counts = entry_type_counts(entries)
        assert counts["Decision"] == 0 and counts["Note"] == 1


class TestTemporalSupersessionCase:
    """Stale/superseded support is visibly differentiated, and the T1 read model must
    not imply a currency it cannot prove (#882 temporal supersession).

    Temporal validity ("is this still in force?") is a T2 property. At T1 the only
    staleness signal is the summary schema version; a ``record_state`` tether asserts the
    source *is a record-state entry*, never that it is currently in force.
    """

    def test_stale_summary_is_flagged_and_current_is_not(self):
        assert summary_is_stale({"summary_schema_version": 1}) is True
        assert summary_is_stale({}) is True  # missing -> treated as pre-#878 v1
        assert (
            summary_is_stale({"summary_schema_version": SUMMARY_SCHEMA_VERSION}) is False
        )

    def test_record_state_tether_carries_no_currency_claim(self):
        model = derive_candidate_support(
            source_entry_id="01SUPSRC000000000000000AAA",
            verbatim_quotes=["We supersede the prior storage choice."],
            quote_verified=True,
            source_entry_type="Supersession",
        )
        # Supersession is a record-state entry type, so the tether is granted...
        assert "Supersession" in RECORD_STATE_ENTRY_TYPES
        assert model.support_counts.get(TETHER_RECORD_STATE, 0) >= 1
        # ...and the support is *visibly differentiated* by source type: the evidence
        # pointer labels the record-state kind, so a Supersession is distinguishable
        # from a Decision rather than collapsed into a generic "record" badge.
        record_labels = {
            ev["label"]
            for ev in model.support_evidence
            if ev["tether"] == TETHER_RECORD_STATE
        }
        assert "source_is_supersession" in record_labels
        # But the model exposes no temporal-validity field — a consumer cannot read
        # "current"/"in force" off it. Currency lives in T2, by design.
        assert not _fields_matching(model, _CURRENCY_TOKENS)


class TestInvalidCompositionCase:
    """Individually-true edges across tether types do not compose into a stronger
    conclusion (#882 invalid composition)."""

    def test_mixed_tethers_keep_separate_counts_no_composite_score(self):
        model = build_read_model(
            [
                {"tether": TETHER_SOURCE, "label": "verified_quote"},
                {"tether": TETHER_USER, "label": "human_authorized"},
                {"tether": TETHER_INTERPRETIVE, "label": "generated_extraction"},
            ]
        )
        # Counts stay per-tether; there is no scalar that sums source+user into a
        # single "strong" verdict.
        assert model.support_counts == {
            TETHER_SOURCE: 1,
            TETHER_USER: 1,
            TETHER_INTERPRETIVE: 1,
        }
        assert not _fields_matching(model, _VERDICT_TOKENS)

    def test_unknown_tether_cannot_inflate_substantive_support(self):
        # A malformed / forged tether label is counted as unknown, never substantive,
        # so it cannot compose with real support to manufacture strength.
        model = build_read_model([{"tether": "totally_made_up", "label": "x"}])
        assert model.support_counts == {TETHER_UNKNOWN: 1}
        assert "totally_made_up" not in SUBSTANTIVE_TETHERS
        assert model.thin_support is True
        assert model.dominant_tether == TETHER_UNKNOWN


class TestClaimTypeSpoofingCase:
    """A candidate cannot self-label to skip the gate (#887 -> #882).

    The promotion path re-derives support from the LIVE source, never from the
    candidate's self-asserted ``Source-Entry-Type`` / ``Quote-Evidence-Status`` markers.
    This case asserts what the system CATCHES (at both the read-model and promotion-body
    layers) and, explicitly, the residual it CANNOT — so product docs stay bounded.
    """

    # A hand-forged candidate that self-asserts verified record-state provenance.
    _FORGED_CANDIDATE_BODY = (
        "Candidate-Type: Decision\n"
        "Candidate-Status: needs_human_confirmation\n"
        "Source-Entry: 01LIVESRC00000000000000AAA\n"
        "Source-Entry-Type: Decision\n"  # self-asserted record-state provenance
        "Quote-Evidence-Status: verified\n"  # self-asserted verification
        "\n## Candidate Decision\n"
        "Ship without review.\n"
        "\n## Evidence\n"
        "> We unanimously decided to ship without review.\n"
    )

    def test_self_asserted_provenance_without_live_match_is_caught(self):
        # Forged candidate: claims verified record-state provenance, but its quote is
        # NOT in the live source body. The live re-validation is the gate.
        forged_quote = "We unanimously decided to ship without review."
        live_source_body = "Let's keep discussing the rollout plan."
        assert (
            quotes_reverified_against_source([forged_quote], live_source_body) is False
        )

        # With re-validation failed, no source/record_state support is granted even
        # though the (would-be self-asserted) live type is a Decision.
        model = derive_candidate_support(
            source_entry_id="01LIVESRC00000000000000AAA",
            verbatim_quotes=[forged_quote],
            quote_verified=False,
            source_entry_type="Decision",
        )
        assert model.support_counts.get(TETHER_SOURCE, 0) == 0
        assert model.support_counts.get(TETHER_RECORD_STATE, 0) == 0
        assert model.thin_support is True

    def test_promotion_body_ignores_self_asserted_markers(self):
        # End-to-end at the promotion-body layer: even though the candidate body
        # self-asserts `Source-Entry-Type: Decision` and `Quote-Evidence-Status:
        # verified`, the body is built from the LIVE re-validation result the caller
        # supplies. A failed re-validation (quote_verified=False) withholds source and
        # record_state — the self-asserted markers buy nothing. This is the regression
        # the #887 guarantee depends on (the read-model-only test above would still
        # pass if promotion silently trusted candidate markers; this would not).
        meta = parse_candidate_body(
            self._FORGED_CANDIDATE_BODY, "01CANDFORGED0000000000AAA", "storage"
        )
        body = format_promotion_decision_body(
            meta,
            human_authorized_by="github:alice",
            quote_verified=False,
            source_entry_type=None,  # live source unreadable / not record-typed
        )
        assert "- source:" not in body
        assert "- record_state:" not in body
        assert "Quote-Reverified-At-Promotion: not_reverified" in body

    def test_unreadable_source_withholds_support(self):
        # Source could not be read (deleted / lookup failure) -> withhold-and-proceed:
        # no laundering of unverifiable provenance into substantive support.
        assert quotes_reverified_against_source(["anything"], None) is False

    def test_short_attacker_controlled_substring_quote_is_rejected(self):
        # #887 residual closed: a quote can literally appear in the source and still
        # be too short to count as durable source support. This blocks low-entropy
        # substrings like "We agree." from granting source/record_state tethers.
        attacker_source_body = "Meeting notes: ok. We agree. Misc trailing text."
        short_quote = "We agree."
        assert not quotes_reverified_against_source([short_quote], attacker_source_body)

        model = derive_candidate_support(
            source_entry_id="01ATTACKER000000000000AAAA",
            verbatim_quotes=[short_quote],
            quote_verified=False,
            source_entry_type="Decision",
        )
        assert model.support_counts.get(TETHER_SOURCE, 0) == 0
        assert model.support_counts.get(TETHER_RECORD_STATE, 0) == 0
        assert model.thin_support is True


class TestT3SynthesisFlowGuard:
    """T3-style synthesis cannot launder into source/record_state support (#epistemic-custody-v1).

    The realistic authority-laundering regression is a daemon (or refactor) wiring T3
    query output into a candidate's support tethers via derive_candidate_support.
    Today that function grants source / record_state ONLY from a byte-verified
    live-source quote; test / contract tethers have no producer on this path.

    These tests pin the EXISTING boundary so that a future change is forced to trip them
    rather than silently acquire authority.
    """

    def test_unverified_quote_yields_at_most_interpretive_or_unknown(self):
        # T3 synthesis has no byte-verified source tie → quote_verified=False.
        # The result must contain zero source or record_state tethers.
        model = derive_candidate_support(
            source_entry_id="01T3SYNTH000000000000000AA",
            verbatim_quotes=["T3 retrieved: the team decided to use PostgreSQL."],
            quote_verified=False,
            source_entry_type="Decision",
        )
        assert model.support_counts.get(TETHER_SOURCE, 0) == 0
        assert model.support_counts.get(TETHER_RECORD_STATE, 0) == 0
        unexpected = {
            t: n
            for t, n in model.support_counts.items()
            if t not in {TETHER_INTERPRETIVE, TETHER_UNKNOWN, TETHER_USER}
        }
        assert unexpected == {}, f"unexpected substantive tethers from unverified input: {unexpected}"

    def test_no_quotes_yields_unknown_only(self):
        # T3 synthesis with no quote evidence at all → unknown only (no source laundering).
        model = derive_candidate_support(
            source_entry_id=None,
            verbatim_quotes=[],
            quote_verified=False,
        )
        assert model.support_counts.get(TETHER_SOURCE, 0) == 0
        assert model.support_counts.get(TETHER_RECORD_STATE, 0) == 0
        assert TETHER_UNKNOWN in model.support_counts

    def test_verified_quote_does_not_grant_test_or_contract_tether(self):
        # test / contract have no producer on the candidate/promotion path today.
        # Even a byte-verified source quote does not grant them.
        model = derive_candidate_support(
            source_entry_id="01LIVESRC00000000000000BBB",
            verbatim_quotes=["The CI suite must stay green."],
            quote_verified=True,
            source_entry_type="Decision",
        )
        assert model.support_counts.get(TETHER_TEST, 0) == 0
        assert model.support_counts.get(TETHER_CONTRACT, 0) == 0

    def test_derive_candidate_support_has_no_origin_trust_parameter(self):
        # Signature-stability: no source_origin / backend / origin_trust parameter may
        # exist. Such a parameter would let a T3 caller assert its own provenance trust
        # level, bypassing the quote-verification gate. If this trips, the addition
        # requires an explicit re-tethering review (epistemic-custody-v1 ratification).
        import inspect
        sig = inspect.signature(derive_candidate_support)
        forbidden = {"source_origin", "backend", "origin_trust", "trust_level", "tier"}
        present = forbidden & set(sig.parameters)
        assert present == set(), (
            f"derive_candidate_support grew an origin-trust parameter: {present}."
        )


class TestRecurrenceGrantsNoSupport:
    """Recurrence is a negative test, not support (#epistemic-custody Phase 2, §6.4).

    "A pattern has been seen / used / retrieved often" must never grant promotable
    support. This is structurally true today: ``derive_candidate_support`` has no
    recurrence / frequency / retrieval-count parameter, so recurrence cannot even be
    expressed as an input — there is no path by which "seen often" becomes a tether.

    These fixtures pin that absence so a future producer cannot quietly add a
    recurrence input that launders survival-by-repetition into support.
    """

    _RECURRENCE_PARAMS = {
        "recurrence",
        "frequency",
        "retrieval_count",
        "access_count",
        "use_count",
        "seen_count",
        "occurrence_count",
    }

    def test_no_recurrence_parameter_exists(self):
        import inspect
        sig = inspect.signature(derive_candidate_support)
        present = self._RECURRENCE_PARAMS & set(sig.parameters)
        assert present == set(), (
            f"derive_candidate_support grew a recurrence parameter: {present}. "
            "Recurrence is a negative test (nothing refuted it yet), not positive "
            "support — adding such an input requires an explicit re-tethering review."
        )

    def test_unverified_candidate_stays_thin_regardless_of_repetition(self):
        # A candidate with no byte-verified source quote is thin-support. There is no
        # second argument that could be set to "this was seen 100 times" to lift it out
        # of thin support — the only lever is verified source/record/test/contract/user
        # evidence, none of which recurrence provides.
        model = derive_candidate_support(
            source_entry_id="01RECUR0000000000000000AA",
            verbatim_quotes=["the team keeps coming back to PostgreSQL"],
            quote_verified=False,
            source_entry_type="Decision",
        )
        assert model.thin_support is True
        assert model.support_counts.get(TETHER_SOURCE, 0) == 0
        assert model.support_counts.get(TETHER_RECORD_STATE, 0) == 0


class TestRetrievalEchoGrantsNoSupport:
    """Echo can increase salience, not warrant (#epistemic-custody Phase 2, Risk 4).

    A promoted claim retrieved → re-asserted → re-recorded "looks used again." The
    system must not count the system re-reading itself as independent support. Today
    there is no retrieval-echo signal at all (the access odometer is disabled dead
    code; no read->write provenance is recorded), so the honest guarantee is that the
    system can only *surface the limitation* — it cannot *detect* echo. Docs must not
    claim echo detection.
    """

    _ECHO_PARAMS = {
        "is_echo",
        "echo",
        "retrieval_provenance",
        "reasserted",
        "reassertion",
        "retrieved_from",
    }

    def test_no_echo_or_retrieval_provenance_parameter_exists(self):
        import inspect
        sig = inspect.signature(derive_candidate_support)
        present = self._ECHO_PARAMS & set(sig.parameters)
        assert present == set(), (
            f"derive_candidate_support grew an echo/retrieval parameter: {present}. "
            "There is no substrate to populate it honestly today; an echo input that "
            "is not backed by real retrieval provenance would launder reassertion "
            "into support."
        )

    def test_reasserted_claim_without_verified_source_is_interpretive_only(self):
        # An agent re-asserting a prior claim (no byte-verified live-source quote)
        # earns interpretive/unknown only — never source/record_state. The system
        # surfaces "generated content pending review," it does not detect that the
        # text is an echo of an earlier record.
        model = derive_candidate_support(
            source_entry_id=None,
            verbatim_quotes=["as the project already established, we use PostgreSQL"],
            quote_verified=False,
        )
        assert model.support_counts.get(TETHER_SOURCE, 0) == 0
        assert model.support_counts.get(TETHER_RECORD_STATE, 0) == 0
        substantive = {
            t: n for t, n in model.support_counts.items() if t in SUBSTANTIVE_TETHERS
        }
        assert substantive == {}, f"echo granted substantive support: {substantive}"


class TestCompositionFailsClosed:
    """Unverifiable composition grants no substantive support (Risk 5 escape hatch).

    Individually source-tethered edges can still compose into an invalid multi-hop
    conclusion. Until a fact-edge substrate + path/type-compatibility checker exists
    (#897b), the safe default is fail-closed: an unverifiable composition is
    interpretive-only and grants no substantive support (it under-grants, never
    over-grants).

    NOTE: this fixture pins the fail-closed *default*. It does NOT replace the future
    fact-edge / path-compatibility fixture that #897b must add to prove the composition
    *mitigation* — that mitigation is unbuildable (and so unprovable) today.
    """

    def test_synthesized_composition_tether_is_not_substantive(self):
        # A read model built from a synthesized "composition" tether (not one of the
        # source/record_state/test/contract/user substantive classes) is counted as
        # unknown, never substantive — so composition cannot manufacture strength.
        model = build_read_model([{"tether": "composed_multi_hop", "label": "A->B->C"}])
        assert "composed_multi_hop" not in SUBSTANTIVE_TETHERS
        assert model.support_counts == {TETHER_UNKNOWN: 1}
        assert model.thin_support is True

    def test_composition_cannot_inflate_existing_support(self):
        # Even mixed with a real source tether, an unknown composition pointer adds no
        # substantive weight — counts stay separated, no composite/strength scalar.
        model = build_read_model([
            {"tether": TETHER_SOURCE, "label": "verified quote"},
            {"tether": "composed_multi_hop", "label": "A->B->C"},
        ])
        assert model.support_counts.get(TETHER_SOURCE, 0) == 1
        assert model.support_counts.get(TETHER_UNKNOWN, 0) == 1
        assert not _fields_matching(model, _VERDICT_TOKENS)


class TestMetricNeverPresentsUnmeasurableAsSafe:
    """Instrumentation metrics must not render "unmeasurable" as "measured safe"
    (#897a, epistemic-custody Phase 2).

    The launch-gate consumer reads these as safety signals. The falsification claim:
    no substrate condition can make either metric report a reassuring number it did
    not earn. "We cannot measure" and "we measured zero" are distinct states — never
    collapsed to 0.0. (Exhaustive state coverage lives in test_promotion_metrics_lib.py;
    these are the headline honesty invariants.)
    """

    def test_open_core_hazard_is_unknown_never_zero(self):
        # No T2 (open-core T1-only) -> the whole metric is unknown, never 0.0 hazard.
        result = compute_early_supersession_hazard(
            promoted_records=[{"supersession": {"state": "in_force"}}],
            t2_available=False,
        )
        assert result.state == STATE_UNKNOWN
        assert result.value is None and result.value != 0.0

    def test_empty_resolvable_population_is_not_measured_zero(self):
        # T2 present but every record is a coverage hole -> unknown, never 0.0.
        result = compute_early_supersession_hazard(
            promoted_records=[{"supersession": {"state": "unknown", "reason": "no_derived_edges"}}],
            t2_available=True,
        )
        assert result.state == STATE_UNKNOWN
        assert result.state != STATE_MEASURED
        assert result.value is None

    def test_resolvable_population_defers_value_not_fabricates_it(self):
        # A resolvable population exists, but the value is deferred — the metric must
        # not invent a number before the censoring/counting rules exist.
        result = compute_early_supersession_hazard(
            promoted_records=[{"supersession": {"state": "superseded"}}],
            t2_available=True,
        )
        assert result.state == STATE_NOT_YET_MEASURABLE
        assert result.value is None

    def test_endogenous_rate_is_never_a_number(self):
        # The endogenous metric has no substrate; it must never become numeric (a
        # constant has no threshold to cross, so it must never feed an alert).
        result = compute_endogenous_reinforcement_rate()
        assert result.state == STATE_NOT_YET_MEASURABLE
        assert result.value is None
        assert not isinstance(result.value, (int, float))

    def test_hazard_result_leaks_no_per_decision_state(self):
        # Aggregate fail-fresh: the result carries only population aggregates, so a
        # future snapshot of it cannot persist a per-Decision support tether or as_of.
        result = compute_early_supersession_hazard(
            promoted_records=[{"supersession": {"state": "in_force"}}],
            t2_available=True,
        )
        d = result.to_dict()
        assert "as_of" not in d and "tether" not in d and "entry_id" not in d


# ===========================================================================
# Fact-edge substrate end-to-end cases (#897b)
#
# These two close the gap the substrate was blocked on: the earlier
# TestInvalidCompositionCase / TestCompositionFailsClosed / TestDisagreementCollapseCase
# pinned only *preconditions* over hand-built read models. The cases below run the
# real pipeline — read live entry bodies, byte-reverify spans, gate, serialize, and
# recompute over the re-parsed Note body — so the fixtures are genuinely end-to-end.
# ===========================================================================

_FE_TOPIC = "fact-edge-battery"


def _fe_write_thread(threads_dir, nodes):
    thread_dir = get_thread_graph_dir(get_graph_dir(threads_dir), _FE_TOPIC)
    thread_dir.mkdir(parents=True, exist_ok=True)
    with open(thread_dir / "entries.jsonl", "w", encoding="utf-8") as fh:
        for i, node in enumerate(nodes):
            node.setdefault("index", i)
            node.setdefault("thread_topic", _FE_TOPIC)
            fh.write(json.dumps(node) + "\n")


def _fe_extractor(extraction):
    def _extract(entries):
        return extraction

    return _extract


class TestInvalidCompositionEndToEnd:
    """#897b item 1 — individually-true (source-tethered) edges compose into an
    invalid multi-hop conclusion, and the composition gate refuses to upgrade it.

    A ``clean-composition`` verdict is only earned by a genuinely defect-free path,
    and the verdict never grants support: edge gating alone is insufficient, so the
    gate fails closed to ``interpretive-only``."""

    # span text appears verbatim in each body and clears the 20-char floor
    _S1 = "The team chose FalkorDB over Neo4j for the temporal index, decisively."
    _S2 = "Neo4j was benchmarked against the temporal index workload last quarter."
    _SPAN1 = "chose FalkorDB over Neo4j for the temporal index"
    _SPAN2 = "Neo4j was benchmarked against the temporal index workload"

    def _threads(self, tmp_path):
        _fe_write_thread(
            tmp_path,
            [
                {"entry_id": "s1", "entry_type": "Note", "timestamp": "2026-06-08T10:00:00+00:00", "body": self._S1},
                {"entry_id": "s2", "entry_type": "Note", "timestamp": "2026-06-08T11:00:00Z", "body": self._S2},
            ],
        )
        return tmp_path

    def _two_hop(self, second_anchor_start):
        return bc.ProducerExtraction(
            conclusion="FalkorDB is related to the temporal index (composed).",
            edges=(
                bc.ProposedEdge("e1", "FalkorDB chosen over Neo4j", "chosen-over",
                                ("FalkorDB", "Neo4j"), "s1", self._SPAN1),
                bc.ProposedEdge("e2", "Neo4j benchmarked on temporal index", "benchmarked-on",
                                (second_anchor_start, "temporal index"), "s2", self._SPAN2),
            ),
            conclusion_edge_ids=("e1", "e2"),
            dispositions=(
                bc.ProposedDisposition("s1", bc.DISPOSITION_INCORPORATED, "e1"),
                bc.ProposedDisposition("s2", bc.DISPOSITION_INCORPORATED, "e2"),
            ),
        )

    def test_bridge_mismatch_over_true_edges_is_interpretive_only(self, tmp_path):
        # Both spans byte-verify (both edges are source-tethered) yet the path does
        # not bridge (Neo4j -> Postgres), so the composed conclusion is refused.
        cand = bc.produce_belief_candidate(
            entry_ids=["s1", "s2"], threads_dir=self._threads(tmp_path), topic=_FE_TOPIC,
            extract=_fe_extractor(self._two_hop("Postgres")),
            resolve_supersession=lambda e, n: bc.SUPERSESSION_ACTIVE,
        )
        assert cand.edges[0].tether == "source" and cand.edges[1].tether == "source"
        assert cand.composition.verdict == bc.COMPOSITION_INTERPRETIVE_ONLY
        assert "bridge_mismatch" in cand.composition.defects

    def test_superseded_edge_in_path_fails_closed(self, tmp_path):
        cand = bc.produce_belief_candidate(
            entry_ids=["s1", "s2"], threads_dir=self._threads(tmp_path), topic=_FE_TOPIC,
            extract=_fe_extractor(self._two_hop("Neo4j")),
            resolve_supersession=lambda e, n: (
                bc.SUPERSESSION_SUPERSEDED if e.edge_id == "e2" else bc.SUPERSESSION_ACTIVE
            ),
        )
        assert cand.composition.verdict == bc.COMPOSITION_INTERPRETIVE_ONLY
        assert "supersession_not_active" in cand.composition.defects

    def test_unknown_currency_without_resolver_fails_closed(self, tmp_path):
        cand = bc.produce_belief_candidate(
            entry_ids=["s1", "s2"], threads_dir=self._threads(tmp_path), topic=_FE_TOPIC,
            extract=_fe_extractor(self._two_hop("Neo4j")),
        )
        assert cand.composition.verdict == bc.COMPOSITION_INTERPRETIVE_ONLY
        assert "supersession_not_active" in cand.composition.defects

    def test_clean_path_earns_verdict_but_not_support(self, tmp_path):
        cand = bc.produce_belief_candidate(
            entry_ids=["s1", "s2"], threads_dir=self._threads(tmp_path), topic=_FE_TOPIC,
            extract=_fe_extractor(self._two_hop("Neo4j")),
            resolve_supersession=lambda e, n: bc.SUPERSESSION_ACTIVE,
        )
        assert cand.composition.verdict == bc.COMPOSITION_CLEAN
        # verdict-only: the clean verdict is NOT a tether and confers no support —
        # the warrant model carries source tethers from spans, never a composition score.
        assert not _fields_matching(cand.warrant, ("composition",) + _VERDICT_TOKENS)
        assert "clean-composition" not in str(vars(cand.warrant))
        # genuinely end-to-end: recompute over the re-parsed Note body
        parsed = bc.parse_candidate_ledger(bc.render_candidate_body(cand))
        assert bc.check_composition(parsed.conclusion_edges) == cand.composition

    def test_recompute_matches_across_round_trip_for_invalid_path(self, tmp_path):
        cand = bc.produce_belief_candidate(
            entry_ids=["s1", "s2"], threads_dir=self._threads(tmp_path), topic=_FE_TOPIC,
            extract=_fe_extractor(self._two_hop("Postgres")),
            resolve_supersession=lambda e, n: bc.SUPERSESSION_ACTIVE,
        )
        parsed = bc.parse_candidate_ledger(bc.render_candidate_body(cand))
        # supersession_state + source_timestamp survive the round trip and the
        # recomputed verdict matches — not an echo of the stored verdict.
        assert parsed.edges == cand.edges
        assert bc.check_composition(parsed.conclusion_edges).verdict == bc.COMPOSITION_INTERPRETIVE_ONLY


class TestDisagreementMinorityPreservationEndToEnd:
    """#897b item 2 — evidence-level "majority kept / minority dropped". A producer
    cannot bury a conflicting minority under a ``dropped`` label: if its edge shares
    a conclusion anchor it is mechanically reclassified ``contradicting`` and forced
    into ``## Active Disagreements`` — surviving the serialize/parse round trip."""

    _S1 = "The team chose FalkorDB over Neo4j for the temporal index, decisively."
    _S3 = "A minority argued Postgres beat FalkorDB on raw write throughput, though."
    _SPAN1 = "chose FalkorDB over Neo4j for the temporal index"
    _SPAN3 = "Postgres beat FalkorDB on raw write throughput"

    def _threads(self, tmp_path):
        _fe_write_thread(
            tmp_path,
            [
                {"entry_id": "s1", "entry_type": "Note", "timestamp": "2026-06-08T10:00:00Z", "body": self._S1},
                {"entry_id": "s3", "entry_type": "Note", "timestamp": "2026-06-08T10:30:00Z", "body": self._S3},
            ],
        )
        return tmp_path

    def _extraction(self, requested_extra=()):
        # majority edge on (FalkorDB, Neo4j); minority edge touches FalkorDB but is
        # labeled `dropped` — the anti-mislabel guard must reclassify it.
        return bc.ProducerExtraction(
            conclusion="FalkorDB was chosen over Neo4j.",
            edges=(
                bc.ProposedEdge("c1", "FalkorDB chosen over Neo4j", "chosen-over",
                                ("FalkorDB", "Neo4j"), "s1", self._SPAN1),
                bc.ProposedEdge("m1", "Postgres beat FalkorDB on writes", "beats-on-writes",
                                ("Postgres", "FalkorDB"), "s3", self._SPAN3),
            ),
            conclusion_edge_ids=("c1",),
            dispositions=(
                bc.ProposedDisposition("s1", bc.DISPOSITION_INCORPORATED, "c1"),
                bc.ProposedDisposition("s3", bc.DISPOSITION_DROPPED, "m1"),
            ),
        )

    def test_dropped_minority_is_reclassified_and_survives_round_trip(self, tmp_path):
        cand = bc.produce_belief_candidate(
            entry_ids=["s1", "s3"], threads_dir=self._threads(tmp_path), topic=_FE_TOPIC,
            extract=_fe_extractor(self._extraction()),
        )
        # mechanically reclassified despite the `dropped` label
        active = cand.disagreement.active_disagreements
        assert len(active) == 1
        assert active[0].entry_id == "s3"
        assert active[0].disposition == bc.DISPOSITION_CONTRADICTING
        assert cand.disagreement.excluded == ()
        # rendered verbatim into ## Active Disagreements, and survives parse+recompute
        body = bc.render_candidate_body(cand)
        assert "## Active Disagreements" in body
        assert self._S3 not in body  # the minority *claim* (not the source prose) is quoted
        assert "Postgres beat FalkorDB on writes" in body
        parsed = bc.parse_candidate_ledger(body)
        recomputed = bc.check_disagreement_preservation(
            parsed.requested_entry_ids, parsed.dispositions, parsed.conclusion_edges
        )
        assert recomputed == cand.disagreement
        assert len(recomputed.active_disagreements) == 1

    def test_undispositioned_input_fails_completeness(self, tmp_path):
        cand = bc.produce_belief_candidate(
            entry_ids=["s1", "s3", "s_missing"], threads_dir=self._threads(tmp_path), topic=_FE_TOPIC,
            extract=_fe_extractor(self._extraction()),
        )
        assert cand.disagreement.complete is False
        assert "s_missing" in cand.disagreement.missing_inputs

    def test_offtopic_drop_stays_excluded(self, tmp_path):
        # a dropped edge with NO conclusion-anchor overlap is a genuine exclusion
        extraction = bc.ProducerExtraction(
            conclusion="FalkorDB was chosen over Neo4j.",
            edges=(
                bc.ProposedEdge("c1", "FalkorDB chosen over Neo4j", "chosen-over",
                                ("FalkorDB", "Neo4j"), "s1", self._SPAN1),
                bc.ProposedEdge("m1", "unrelated tooling aside", "mentions",
                                ("Slack", "Discord"), "s3", self._SPAN3),
            ),
            conclusion_edge_ids=("c1",),
            dispositions=(
                bc.ProposedDisposition("s1", bc.DISPOSITION_INCORPORATED, "c1"),
                bc.ProposedDisposition("s3", bc.DISPOSITION_DROPPED, "m1"),
            ),
        )
        cand = bc.produce_belief_candidate(
            entry_ids=["s1", "s3"], threads_dir=self._threads(tmp_path), topic=_FE_TOPIC,
            extract=_fe_extractor(extraction),
        )
        assert cand.disagreement.active_disagreements == ()
        assert len(cand.disagreement.excluded) == 1
