"""Tests for the anti-anthropomorphism interiority lint (#895).

The lint flags first-person *interiority* (belief/care/concern/conscience)
while leaving procedural first-person, scoped idioms, quoted evidence, and
embedded code untouched. Conservatism is a correctness property here: a false
positive on ordinary engineering text trains callers to ignore the advisory.
"""

from __future__ import annotations

import pytest

from watercooler.anthropomorphism_lint import (
    ANTHROPOMORPHISM_ADVISORY_MARKER,
    CATEGORY_BELIEF,
    CATEGORY_CARE_CONCERN,
    CATEGORY_CONSCIENCE,
    InteriorityAssessment,
    lint_interiority,
    render_advisory_marker,
)


def _categories(assessment: InteriorityAssessment) -> set[str]:
    return {f.category for f in assessment.findings}


class TestFlagsInteriority:
    @pytest.mark.parametrize(
        "body,category",
        [
            ("I believe the cache is the bottleneck.", CATEGORY_BELIEF),
            ("I don't believe this migration is safe.", CATEGORY_BELIEF),
            ("I do not believe the lock is reentrant.", CATEGORY_BELIEF),
            ("I strongly suspect a deadlock.", CATEGORY_BELIEF),
            ("I genuinely believe we should roll back.", CATEGORY_BELIEF),
            ("I'm convinced this migration is safe.", CATEGORY_BELIEF),
            ("I'm confident the deploy is clean.", CATEGORY_BELIEF),
            ("I'm not convinced this is safe.", CATEGORY_BELIEF),
            ("I'm not sure the lock is reentrant.", CATEGORY_BELIEF),
            ("I am not confident in the deploy.", CATEGORY_BELIEF),
            ("In my opinion we should defer the rewrite.", CATEGORY_BELIEF),
            ("My judgment is that Postgres fits better.", CATEGORY_BELIEF),
            ("I feel strongly that we should roll back.", CATEGORY_BELIEF),
            ("I'm worried about the latency regression.", CATEGORY_CARE_CONCERN),
            ("I'm concerned about dropped messages.", CATEGORY_CARE_CONCERN),
            ("I'm not worried about latency.", CATEGORY_CARE_CONCERN),
            ("I'm not concerned about the cutover.", CATEGORY_CARE_CONCERN),
            ("I worry the queue will back up under load.", CATEGORY_CARE_CONCERN),
            ("I care deeply about getting the rollback right.", CATEGORY_CARE_CONCERN),
            ("I deeply care about correctness here.", CATEGORY_CARE_CONCERN),
            ("My concern is data loss during the cutover.", CATEGORY_CARE_CONCERN),
            ("I'm afraid the build is broken.", CATEGORY_CARE_CONCERN),
            ("I can't in good conscience approve this.", CATEGORY_CONSCIENCE),
            ("My conscience says we should warn users first.", CATEGORY_CONSCIENCE),
        ],
    )
    def test_interiority_is_flagged(self, body, category):
        assessment = lint_interiority(body)
        assert assessment.advisory is True
        assert category in _categories(assessment)

    def test_curly_apostrophe_still_fires(self):
        # U+2019 RIGHT SINGLE QUOTATION MARK, as emitted by smart-quote tooling.
        assessment = lint_interiority("I’m worried about the rollout.")
        assert assessment.advisory is True
        assert CATEGORY_CARE_CONCERN in _categories(assessment)


class TestDoesNotFlagProceduralFirstPerson:
    @pytest.mark.parametrize(
        "body",
        [
            "I searched the threads and found three matches.",
            "I ran the quote check and it passed.",
            "I read the config file and updated the timeout.",
            "I found a bug in the retry loop and fixed it.",
            "I queried T2 and the edge was already invalidated.",
            "I added a test and it now reproduces the failure.",
            "I checked the schema and the column is present.",
            # "understand" is procedural-comprehension here — the whole category
            # is intentionally unshipped (see module note).
            "I understand the stack trace and patched it.",
            "As I understand it, the API returns 404 on missing keys.",
            "My understanding is the cache TTL is 60s.",
        ],
    )
    def test_procedural_first_person_is_clean(self, body):
        assert lint_interiority(body).advisory is False


class TestDoesNotFlagScopedIdioms:
    """Forms that share a stem with interiority but mean something procedural."""

    @pytest.mark.parametrize(
        "body",
        [
            "The module I'm concerned with is parse_config().",
            "These are the files I'm concerned with in this PR.",
            # Negation must not defeat the scope-idiom exclusion.
            "I'm not concerned with parse_config() in this PR.",
            "I care about the test suite passing before merge.",
            "I don't care which linter we use, both work.",
            "I'm sure-footed about the deploy plan.",
            "I'm afraid not — that approach won't work.",
            "I'm afraid so; the regression is real.",
        ],
    )
    def test_scoped_idioms_are_clean(self, body):
        assert lint_interiority(body).advisory is False


class TestDoesNotLintQuotedOrCode:
    """Quoted evidence and embedded code are not the author's own voice."""

    def test_inline_code_span_is_skipped(self):
        body = "I searched for `I believe` and found one fixture."
        assert lint_interiority(body).advisory is False

    def test_blockquote_line_is_skipped(self):
        body = "Summarizing the prior entry:\n> I believe the cache is at fault.\nThat was the claim."
        assert lint_interiority(body).advisory is False

    def test_fenced_code_block_is_skipped(self):
        body = "Here is the fixture:\n```\nbody = \"I'm worried\"\n```\nIt asserts the marker."
        assert lint_interiority(body).advisory is False

    def test_real_voice_outside_code_still_fires(self):
        # Stripping must not swallow genuine interiority next to a code span.
        body = "I'm worried about this: the call to `parse()` can raise."
        assert lint_interiority(body).advisory is True


class TestDoesNotFlagImpersonalProse:
    @pytest.mark.parametrize(
        "body",
        [
            "This analysis suggests the cache is the bottleneck.",
            "The retrieved evidence indicates a latency regression.",
            "A risk is data loss during the cutover.",
            "A concern is that the queue backs up under load.",
            "The concern here is throughput, not correctness.",
            "A human decision is needed before promotion.",
            "We chose Postgres for session storage.",
            "Understanding the schema requires reading the migration.",
        ],
    )
    def test_impersonal_prose_is_clean(self, body):
        assert lint_interiority(body).advisory is False


class TestAssessmentShape:
    def test_empty_body_is_not_advisory(self):
        assert lint_interiority("").advisory is False
        assert lint_interiority(None).advisory is False
        assert lint_interiority("   \n  ").advisory is False

    def test_one_finding_per_category(self):
        # Two belief constructions → a single belief finding (first wins).
        body = "I believe this is right. I suspect it too."
        assessment = lint_interiority(body)
        assert _categories(assessment) == {CATEGORY_BELIEF}
        assert len(assessment.findings) == 1

    def test_multiple_categories_collected(self):
        body = "I'm worried about latency, and I believe the cache is at fault."
        assessment = lint_interiority(body)
        assert _categories(assessment) == {CATEGORY_CARE_CONCERN, CATEGORY_BELIEF}

    def test_finding_carries_verbatim_span_and_suggestion(self):
        finding = lint_interiority("I believe we should ship.").findings[0]
        assert finding.match.lower() == "i believe"
        assert "analysis suggests" in finding.suggestion


class TestRenderAdvisoryMarker:
    def test_clean_assessment_renders_empty(self):
        assert render_advisory_marker(InteriorityAssessment(advisory=False)) == ""

    def test_marker_is_structured_and_queryable(self):
        rendered = render_advisory_marker(lint_interiority("I'm worried about this."))
        first_line = rendered.splitlines()[0]
        assert first_line == f"{ANTHROPOMORPHISM_ADVISORY_MARKER}: interiority"

    def test_note_quotes_span_and_states_non_blocking(self):
        rendered = render_advisory_marker(lint_interiority("I believe this is right."))
        assert '"I believe"' in rendered
        assert "not blocked" in rendered
        assert "operational labels for generated contributions" in rendered

    def test_multi_category_note_dedupes_suggestions(self):
        rendered = render_advisory_marker(
            lint_interiority("I believe X. I'm worried about Y.")
        )
        # Two distinct spans present.
        assert '"I believe"' in rendered
        assert '"I' in rendered and "worried" in rendered
