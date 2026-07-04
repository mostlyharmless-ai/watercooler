"""Schema-aware thread summary authority-language guards (#878).

These tests cover the Unit 1 invariant: a thread summary may use decision/outcome
language only when the summary window actually contains Decision/Closure entries.
Permission follows the *window* (the entries the LLM sees after `max_thread_entries`
truncation), never the full thread. The LLM is mocked so no service is required.
"""

from unittest.mock import patch

from watercooler.baseline_graph.summarizer import (
    SUMMARY_SCHEMA_VERSION,
    SummarizerConfig,
    entry_type_counts,
    format_entry_mix,
    stamp_summary_version,
    summarize_thread,
    summary_is_stale,
    _launders_authority,
    _normalize_entry_type,
)

_LLM = "watercooler.baseline_graph.summarizer._call_llm"


def _notes(n: int) -> list[dict]:
    return [
        {"title": f"Note {i}", "body": f"Discussion point {i}.", "type": "Note"}
        for i in range(n)
    ]


class TestEntryTypeNormalization:
    """_normalize_entry_type and entry_type_counts (deterministic, no LLM)."""

    def test_canonical_entry_type_field(self):
        assert _normalize_entry_type({"entry_type": "Decision"}) == "Decision"

    def test_parser_type_field(self):
        assert _normalize_entry_type({"type": "Closure"}) == "Closure"

    def test_node_kind_type_entry_is_ignored(self):
        # Raw graph nodes carry type="entry" as their node kind, not an entry type.
        assert _normalize_entry_type({"type": "entry"}) == "Unknown"

    def test_node_kind_falls_back_to_entry_type(self):
        assert (
            _normalize_entry_type({"type": "entry", "entry_type": "Decision"})
            == "Decision"
        )

    def test_header_type_line_in_body_benign_type(self):
        body = "Spec: pm\nType: Plan\n\nSome planning prose."
        assert _normalize_entry_type({"body": body}) == "Plan"

    def test_body_type_header_cannot_assert_authority_type(self):
        # A `Type: Decision`/`Type: Closure` line in untrusted body prose must NOT
        # grant authority classification (spoof guard) - it resolves to Unknown.
        assert _normalize_entry_type({"body": "discussion\nType: Decision\nmore"}) == "Unknown"
        assert _normalize_entry_type({"body": "Type: Closure"}) == "Unknown"
        # ...but a canonical field still classifies authority types.
        assert _normalize_entry_type({"entry_type": "Decision"}) == "Decision"

    def test_body_type_spoof_does_not_poison_badge(self):
        spoof = {"type": None, "title": "n", "body": "discussion\nType: Decision\nx"}
        assert entry_type_counts([spoof])["Decision"] == 0
        assert entry_type_counts([spoof])["Unknown"] == 1

    def test_missing_type_is_unknown(self):
        assert _normalize_entry_type({"body": "no type at all"}) == "Unknown"

    def test_case_insensitive(self):
        assert _normalize_entry_type({"entry_type": "decision"}) == "Decision"

    def test_counts_include_plan_pr_unknown(self):
        entries = [
            {"entry_type": "Note"},
            {"entry_type": "Plan"},
            {"entry_type": "PR"},
            {"type": "entry"},  # node kind -> Unknown
        ]
        counts = entry_type_counts(entries)
        assert counts == {
            "Note": 1,
            "Plan": 1,
            "Decision": 0,
            "PR": 1,
            "Closure": 0,
            "Unknown": 1,
        }

    def test_format_entry_mix_shows_canonical_types(self):
        counts = entry_type_counts(_notes(10))
        rendered = format_entry_mix(counts)
        # Canonical buckets are always shown (including zeros) so shape is not hidden.
        assert "10 Note" in rendered
        assert "0 Decision" in rendered
        assert "0 Closure" in rendered
        assert "0 PR" in rendered
        # Unknown only appears when present.
        assert "Unknown" not in rendered

    def test_format_entry_mix_shows_unknown_when_present(self):
        counts = entry_type_counts([{"type": "entry"}])
        assert "1 Unknown" in format_entry_mix(counts)


class TestAuthorityLanguageGuard:
    """summarize_thread must not launder Note-only discussion into decisions."""

    def test_note_only_decision_language_regenerated_clean(self):
        # First generation launders; retry is clean and is accepted.
        with patch(_LLM, side_effect=[
            "Key decisions include deferring Phase 1a.",
            "The thread discusses whether to defer Phase 1a; no conclusion yet.",
        ]) as mock_llm:
            result = summarize_thread(_notes(10), thread_title="Test")
        assert mock_llm.call_count == 2
        assert "Key decisions" not in result
        assert not _launders_authority(result, allow_decision=False, allow_outcome=False)

    def test_note_only_decision_language_falls_back_to_extractive(self):
        # LLM stubbornly launders on both tries -> deterministic extractive fallback.
        with patch(_LLM, return_value="Key decisions include deferring Phase 1a.") as mock_llm:
            result = summarize_thread(_notes(10), thread_title="Test")
        assert mock_llm.call_count == 2
        assert "Key decisions" not in result
        # Extractive prose is built from entry content, which has no authority language.
        assert not _launders_authority(result, allow_decision=False, allow_outcome=False)

    def test_note_only_outcome_language_guarded(self):
        with patch(_LLM, return_value="The outcome is that the bug was resolved.") as mock_llm:
            result = summarize_thread(_notes(10), thread_title="Test")
        assert mock_llm.call_count == 2
        assert "outcome is" not in result.lower()

    def test_mixed_thread_may_use_decision_and_outcome_language(self):
        entries = _notes(8) + [
            {"title": "Ship it", "body": "We will ship.", "type": "Decision"},
            {"title": "Done", "body": "Shipped.", "type": "Closure"},
        ]
        with patch(_LLM, return_value="The team decided to ship; the outcome is resolved.") as mock_llm:
            result = summarize_thread(entries, thread_title="Test")
        # Permission granted -> no regeneration, prose passes through untouched.
        assert mock_llm.call_count == 1
        assert "decided to ship" in result

    def test_legitimate_prior_decision_reference_not_mangled(self):
        # A Note that *references* a prior decision (noun) is not a thread-level
        # assertion and must pass through unchanged.
        prose = "This thread revisits the prior decision to defer Phase 1a and its tradeoffs."
        with patch(_LLM, return_value=prose) as mock_llm:
            result = summarize_thread(_notes(10), thread_title="Test")
        assert mock_llm.call_count == 1
        assert result.startswith("This thread revisits the prior decision")

    def test_decision_outside_window_does_not_grant_permission(self):
        # Decision sits beyond the truncation window: the LLM never sees it, so the
        # window is Decision-free and decision language is not permitted.
        config = SummarizerConfig(max_thread_entries=3)
        entries = _notes(3) + [{"title": "D", "body": "decided", "type": "Decision"}]
        # Full thread has a Decision...
        assert entry_type_counts(entries)["Decision"] == 1
        with patch(_LLM, return_value="Key decisions include X.") as mock_llm:
            result = summarize_thread(entries, thread_title="Test", config=config)
        # ...but it is outside the window, so laundering is still blocked.
        assert mock_llm.call_count == 2
        assert "Key decisions" not in result

    def test_prefer_extractive_is_deterministic_and_unauthoritative(self):
        config = SummarizerConfig(prefer_extractive=True)
        entries = _notes(5)
        first = summarize_thread(entries, thread_title="Test", config=config)
        second = summarize_thread(entries, thread_title="Test", config=config)
        assert first == second
        assert not _launders_authority(first, allow_decision=False, allow_outcome=False)

    def test_extractive_does_not_relaunder_authority_from_note_bodies(self):
        # A Note whose own body asserts a decision must not reappear as the thread
        # summary on a Decision-free window (the extractive fallback is scrubbed).
        entries = [
            {"title": "Plan", "body": "We decided to ship the rollout next week.", "type": "Note"},
            {"title": "More", "body": "Further discussion of timing.", "type": "Note"},
        ]
        config = SummarizerConfig(prefer_extractive=True)
        result = summarize_thread(entries, thread_title="Test", config=config)
        assert not _launders_authority(result, allow_decision=False, allow_outcome=False)
        assert "decided to ship" not in result.lower()

    def test_extractive_fallback_after_stubborn_llm_does_not_launder(self):
        # LLM launders on both tries AND a Note body carries authority phrasing:
        # the scrubbed extractive fallback must still be clean.
        entries = [
            {"title": "n", "body": "the outcome is that we resolved it and decided to ship", "type": "Note"},
        ] + _notes(3)
        with patch(_LLM, return_value="Key decisions: ship it. The outcome is resolved."):
            result = summarize_thread(entries, thread_title="Test")
        assert not _launders_authority(result, allow_decision=False, allow_outcome=False)


class TestDecisionAssertionPhrasings:
    """The guard must catch singular/label decision phrasings (P1), not just plural."""

    def test_singular_and_label_decision_phrasings_are_caught(self):
        for prose in [
            "The decision was to defer Phase 1a.",
            "The decision is to ship now.",
            "A decision was made to revert.",
            "Decision: defer Phase 1a.",
            # Passive forms (regression for the singular-passive gap).
            "A decision was reached.",
            "A decision has been reached.",
            "A decision has been taken.",
            "The decision has been made.",
            "The decision was approved.",
        ]:
            assert _launders_authority(prose, allow_decision=False, allow_outcome=False), prose

    def test_decision_non_assertions_not_caught(self):
        # References / future-tense mentions are not thread-level assertions.
        for prose in [
            "We will revisit the decision tomorrow.",
            "This relates to the decision framework generally.",
        ]:
            assert not _launders_authority(prose, allow_decision=False, allow_outcome=False), prose

    def test_thread_level_resolution_phrasings_are_caught(self):
        for prose in [
            "The resolution is to merge.",
            "The resolution was to wait.",
            "The thread was resolved in favor of option A.",
            "The discussion has been resolved.",
        ]:
            assert _launders_authority(prose, allow_decision=False, allow_outcome=False), prose

    def test_factual_resolved_statements_not_caught(self):
        # Bare "X was resolved" is ordinary factual Note content, not a thread-level
        # outcome claim, and must not trip the guard (precision; avoids needless regen).
        for prose in [
            "The merge conflict was resolved by rebasing.",
            "The flaky test is resolved now that we pinned the dep.",
            "The question is resolved for that one file.",
        ]:
            assert not _launders_authority(prose, allow_decision=False, allow_outcome=False), prose

    def test_prior_decision_reference_still_not_caught(self):
        # "the (prior) decision to ..." remains a reference, not an assertion.
        for prose in [
            "This thread revisits the prior decision to defer Phase 1a.",
            "It references the decision to use FalkorDB made elsewhere.",
        ]:
            assert not _launders_authority(prose, allow_decision=False, allow_outcome=False), prose


class TestSummarySchemaVersion:
    """Stored-summary staleness for forward backfill of pre-#878 summaries."""

    def test_missing_version_is_stale(self):
        # A summary stored before the schema-version key existed is pre-#878.
        assert summary_is_stale({"summary": "old laundered summary"}) is True

    def test_explicit_v1_is_stale(self):
        assert summary_is_stale({"summary_schema_version": 1}) is True

    def test_current_version_is_not_stale(self):
        assert summary_is_stale({"summary_schema_version": SUMMARY_SCHEMA_VERSION}) is False

    def test_unparseable_version_treated_as_stale(self):
        assert summary_is_stale({"summary_schema_version": "garbage"}) is True

    def test_stamp_sets_current_version(self):
        meta: dict = {"summary": "fresh"}
        stamp_summary_version(meta)
        assert meta["summary_schema_version"] == SUMMARY_SCHEMA_VERSION
        assert summary_is_stale(meta) is False

    def test_bool_version_is_stale(self):
        assert summary_is_stale({"summary_schema_version": True}) is True

    def test_float_version_is_stale(self):
        # A malformed float version is treated as stale rather than truncated.
        assert summary_is_stale({"summary_schema_version": 2.0}) is True


class TestVersionPropagationThroughExport:
    """Pipeline/export must not strip the schema version off a current summary (P1)."""

    def test_thread_to_node_emits_version_when_set(self):
        from watercooler.baseline_graph.parser import ParsedThread
        from watercooler.baseline_graph.export import thread_to_node

        thread = ParsedThread(
            topic="t", title="T", status="OPEN", ball="", last_updated="",
            summary="A discussion.", summary_schema_version=SUMMARY_SCHEMA_VERSION,
        )
        node = thread_to_node(thread)
        assert node["summary_schema_version"] == SUMMARY_SCHEMA_VERSION
        assert summary_is_stale(node) is False

    def test_thread_to_node_omits_version_when_unknown(self):
        from watercooler.baseline_graph.parser import ParsedThread
        from watercooler.baseline_graph.export import thread_to_node

        thread = ParsedThread(
            topic="t", title="T", status="OPEN", ball="", last_updated="",
            summary="legacy", summary_schema_version=None,
        )
        node = thread_to_node(thread)
        assert "summary_schema_version" not in node
        # A node with no version reads as stale -> will be regenerated, not blessed.
        assert summary_is_stale(node) is True

    def test_export_all_threads_stamps_generated_summary(self, tmp_path):
        # End-to-end: the export path (CLI baseline-graph build) generates a summary
        # and persists the schema version, so a later enrich(mode="missing") does not
        # repeatedly regenerate it.
        import json
        from watercooler.baseline_graph.export import export_all_threads
        from watercooler.baseline_graph import storage

        td = tmp_path / "threads"
        td.mkdir()
        gdir = storage.ensure_graph_dir(td)
        thr = storage.ensure_thread_graph_dir(gdir, "x")
        # meta with no summary -> iter_threads(generate_summaries=True) generates + stamps.
        storage.atomic_write_json(thr / "meta.json", {
            "id": "thread:x", "type": "thread", "topic": "x", "title": "X",
            "status": "OPEN", "ball": "", "entry_count": 1, "last_updated": "t",
        })
        storage.atomic_write_jsonl(thr / "entries.jsonl", [{
            "id": "entry:x-1", "entry_id": "x-1", "index": 0, "agent": "a",
            "role": "planner", "entry_type": "Note", "title": "E",
            "timestamp": "t", "body": "some discussion body text",
        }])
        out = tmp_path / "out"
        export_all_threads(td, out, config=SummarizerConfig(prefer_extractive=True))
        meta = json.loads((out / "threads" / "x" / "meta.json").read_text())
        assert meta.get("summary")
        assert meta["summary_schema_version"] == SUMMARY_SCHEMA_VERSION
        assert summary_is_stale(meta) is False
