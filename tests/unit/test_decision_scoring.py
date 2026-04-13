"""Tests for watercooler.decision_scoring — shared scoring library."""

from __future__ import annotations

from watercooler.decision_scoring import score_entry, score_entries


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _entry(
    *,
    entry_id: str = "01TEST",
    thread_topic: str = "test-topic",
    entry_type: str = "Note",
    title: str = "",
    summary: str = "",
    search_hit: bool = False,
    t2_signal: bool = False,
) -> dict:
    return {
        "entry_id": entry_id,
        "thread_topic": thread_topic,
        "entry_type": entry_type,
        "title": title,
        "summary": summary,
        "search_hit": search_hit,
        "t2_signal": t2_signal,
    }


# ---------------------------------------------------------------------------
# score_entry — basic scoring
# ---------------------------------------------------------------------------


class TestScoreEntry:
    def test_typed_decision_gets_base_3(self):
        result = score_entry(_entry(entry_type="Decision"))
        assert result["score"] >= 3
        assert "typed" in result["signals"]

    def test_strong_phrase_in_summary(self):
        result = score_entry(_entry(summary="we decided to use PostgreSQL"))
        assert result["score"] >= 2
        assert "explicit" in result["signals"]
        assert "we decided" in result["matched_phrases"]

    def test_weak_phrase_in_summary(self):
        result = score_entry(_entry(summary="we chose React for the frontend"))
        assert result["score"] >= 1
        assert "implied" in result["signals"]

    def test_intent_phrase_in_summary(self):
        result = score_entry(_entry(summary="we will migrate to the new API"))
        assert result["score"] >= 1
        assert "intent" in result["signals"]

    def test_search_hit_adds_one(self):
        base = score_entry(_entry(summary="we decided to go"))
        with_hit = score_entry(_entry(summary="we decided to go", search_hit=True))
        assert with_hit["score"] == base["score"] + 1
        assert "search_hit" in with_hit["signals"]

    def test_t2_signal_with_commitment(self):
        result = score_entry(
            _entry(summary="we decided to use X", t2_signal=True)
        )
        assert "t2_state_change_boosted" in result["signals"]

    def test_t2_signal_without_commitment(self):
        result = score_entry(
            _entry(summary="some generic note", t2_signal=True)
        )
        # Should get +1 flat, not +2 boosted
        assert "t2_state_change" in result["signals"]
        assert "t2_state_change_boosted" not in result["signals"]

    def test_question_title_penalty(self):
        result = score_entry(_entry(title="Should we use PostgreSQL?"))
        assert "question_penalty" in result["signals"]

    def test_speculative_penalty(self):
        result = score_entry(_entry(summary="we might consider using Redis"))
        assert "speculative" in result["signals"]
        assert result["score"] == 0  # speculative -1, might get 0 floor

    def test_score_floor_at_zero(self):
        result = score_entry(
            _entry(title="Should we?", summary="considering options")
        )
        assert result["score"] >= 0


# ---------------------------------------------------------------------------
# score_entry — tier classification
# ---------------------------------------------------------------------------


class TestTierClassification:
    def test_low_tier(self):
        result = score_entry(_entry(summary="just a regular note"))
        assert result["tier"] == "Low"
        assert result["score"] < 2

    def test_medium_tier(self):
        result = score_entry(_entry(summary="we decided to use X"))
        assert result["tier"] == "Medium"
        assert 2 <= result["score"] < 4

    def test_high_tier(self):
        result = score_entry(
            _entry(entry_type="Decision", summary="we decided to use X")
        )
        assert result["tier"] == "High"
        assert result["score"] >= 4


# ---------------------------------------------------------------------------
# score_entry — edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_none_title(self):
        e = _entry()
        e["title"] = None
        result = score_entry(e)
        assert result["score"] >= 0

    def test_empty_summary(self):
        result = score_entry(_entry(summary=""))
        assert result["score"] >= 0

    def test_missing_entry_id(self):
        e = _entry()
        del e["entry_id"]
        result = score_entry(e)
        assert result["entry_id"] == ""

    def test_int_entry_id_coerced_to_str(self):
        e = _entry()
        e["entry_id"] = 12345
        result = score_entry(e)
        assert result["entry_id"] == "12345"

    def test_none_entry_id_coerced_to_empty(self):
        e = _entry()
        e["entry_id"] = None
        result = score_entry(e)
        assert result["entry_id"] == ""

    def test_long_summary_truncated(self):
        long_text = "x" * 200
        result = score_entry(_entry(summary=long_text))
        assert len(result["summary"]) < 200
        assert result["summary"].endswith("...")


# ---------------------------------------------------------------------------
# score_entry — negation guard
# ---------------------------------------------------------------------------


class TestNegationGuard:
    def test_negated_strong_phrase(self):
        result = score_entry(
            _entry(summary="we have not decided on a framework yet")
        )
        assert "explicit" not in result["signals"]

    def test_pre_negation(self):
        result = score_entry(
            _entry(summary="they didn't commit to anything")
        )
        # "committed to" should be negated by "didn't"
        assert "explicit" not in result["signals"]


# ---------------------------------------------------------------------------
# score_entry — fuzzy threshold parameterization
# ---------------------------------------------------------------------------


class TestFuzzyThreshold:
    def test_disabled_when_zero(self):
        result = score_entry(
            _entry(summary="we decideed to use X"),
            fuzzy_threshold=0,
        )
        # Typo "decideed" shouldn't match with fuzzy disabled
        assert "explicit_fuzzy" not in result["signals"]

    def test_default_threshold(self):
        # With default threshold, exact matches should still work
        result = score_entry(
            _entry(summary="we decided to use X"),
            fuzzy_threshold=85,
        )
        assert "explicit" in result["signals"]


# ---------------------------------------------------------------------------
# score_entries — batch function
# ---------------------------------------------------------------------------


class TestScoreEntries:
    def test_basic_batch(self):
        entries = [
            _entry(entry_id="1", summary="we decided to use X"),
            _entry(entry_id="2", summary="regular note"),
            _entry(entry_id="3", entry_type="Decision", summary="finalized the API"),
        ]
        results = score_entries(entries, min_score=1)
        assert len(results) >= 1
        # Should be sorted by score descending
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_min_score_filter(self):
        entries = [
            _entry(entry_id="1", summary="we decided to use X"),
            _entry(entry_id="2", summary="regular note"),
        ]
        results = score_entries(entries, min_score=2)
        assert all(r["score"] >= 2 for r in results)

    def test_skip_ids(self):
        entries = [
            _entry(entry_id="skip-me", summary="we decided to use X"),
            _entry(entry_id="keep-me", summary="we decided to use Y"),
        ]
        results = score_entries(entries, skip_ids={"skip-me"})
        assert all(r["entry_id"] != "skip-me" for r in results)

    def test_fuzzy_threshold_passed_through(self):
        entries = [_entry(entry_id="1", summary="we decided to use X")]
        results = score_entries(entries, fuzzy_threshold=0)
        # Should still score via exact match
        assert len(results) >= 1

    def test_empty_input(self):
        assert score_entries([]) == []
