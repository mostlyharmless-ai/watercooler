"""Hosted-mode read path for watercooler_promote_candidate.

In hosted mode `context.threads_dir` is the `/hosted` sentinel — there is no
local filesystem baseline graph. Before the hosted branch, the promote tool ran
the local path unconditionally: `get_graph_dir("/hosted")` → `/hosted/graph/
baseline`, which never exists, so every hosted promotion failed with
"baseline graph not found … Has the thread been read at least once?" *before*
it could find the candidate. (Observed live: a learning-candidate accept on the
dashboard surfaced exactly that error once the masked-success bug was fixed.)

These tests pin that hosted mode reads the candidate + the double-promotion
guard's entry list via `load_thread_entries_hosted` (GitHub-backed), and never
touches the filesystem graph. `say`/disposition already worked in hosted mode —
only the read side was unported.
"""

from __future__ import annotations

import types
from unittest.mock import MagicMock, patch

from watercooler_mcp.tools.promotion import _promote_candidate_impl
from watercooler_mcp.validation import HOSTED_MODE_SENTINEL

_TOPIC = "test-sync-refactor-04"
_CAND = "01KVD2ZHEWMRXBVMYSQBFHBZBR"  # the real candidate that surfaced the bug

_LEARNING_BODY = """\
Spec: learnings
[automated: learnings]
Candidate-Type: Learning
Candidate-Status: needs_human_confirmation
Surface-Kind: learning
Authority: none
Confidence: 4/5
Source-Thread: test-sync-refactor-04
PRs: #81

## Candidate learning
Simplify sync processes to avoid conflicts and improve maintainability

## Why this is a candidate, not a durable learning
Synthesized by the Learnings daemon from a capture-gap thread.

## Root cause
Complexity in managing state file conflicts and shadowed functions during sync.

## Fix
Refactored to eliminate shadowed functions and add conflict resolution.

## Evidence (verbatim)
> refactor(sync): remove shadowed functions from git_sync.py
> refactor(sync): complete migration to sync/ package
"""


def _hosted_context():
    return types.SimpleNamespace(
        threads_dir=HOSTED_MODE_SENTINEL,
        code_repo="mostlyharmless-ai/watercooler",
        code_branch="main",
    )


def _thread_entry(entry_id, body, *, entry_type="Note", title="x"):
    return types.SimpleNamespace(
        entry_id=entry_id, body=body, entry_type=entry_type, title=title
    )


def _say_response(entry_id, entry_type="Note"):
    return (
        f"✅ Entry added to '{_TOPIC}'\n"
        f"Role: implementer | Type: {entry_type}\n"
        f"Status: OPEN\n"
        f"Entry-ID: {entry_id}\n"
        "Ball: caleb. Next: continue."
    )


def _run_hosted_promote(hosted_entries):
    """Drive _promote_candidate_impl through the hosted path; capture say calls."""
    say_calls: list[dict] = []
    # Valid Crockford base32 ULIDs (no I/L/O/U) so _parse_entry_id accepts them.
    lesson_id = "01HZA9T0BC3D4E5F6G7H8J9K0M"
    disp_id = "01HZAAT0BC3D4E5F6G7H8J9K0M"

    def fake_say(*, topic, title, body, entry_type, role, agent_func, **kw):
        say_calls.append({"entry_type": entry_type, "body": body, "title": title})
        return _say_response(
            lesson_id if len(say_calls) == 1 else disp_id, entry_type=entry_type
        )

    with (
        patch("watercooler_mcp.tools.promotion._say_impl", side_effect=fake_say),
        patch(
            "watercooler_mcp.validation._require_context",
            return_value=(None, _hosted_context()),
        ),
        patch(
            "watercooler_mcp.hosted_ops.load_thread_entries_hosted",
            return_value=(None, hosted_entries),
        ) as load_hosted,
        # If the hosted branch is wrong and the code falls through to the local
        # filesystem read, this would be hit — assert it is NOT.
        patch(
            "watercooler_mcp.tools.promotion.get_entry_node_from_graph",
            side_effect=AssertionError("filesystem graph must not be read in hosted mode"),
        ),
    ):
        result = _promote_candidate_impl(
            candidate_entry_id=_CAND,
            topic=_TOPIC,
            target_type="Learning",
            human_authorized_by="github:calebjacksonhoward",
            ctx=MagicMock(),
            code_path="mostlyharmless-ai/watercooler",
            agent_func="Claude Code:claude-opus-4-8:implementer",
        )
    return result, say_calls, load_hosted


class TestHostedPromoteReadPath:
    def test_hosted_learning_promote_succeeds_via_github_read(self):
        entries = [
            _thread_entry("01OTHER000000000000000000A", "some other entry"),
            _thread_entry(_CAND, _LEARNING_BODY, title="Learning candidate"),
        ]
        result, say_calls, load_hosted = _run_hosted_promote(entries)

        assert result.startswith("✅"), result
        assert "to Learning" in result
        # Read came from the hosted/GitHub path, scoped to the topic.
        load_hosted.assert_called_once_with(_TOPIC)
        # Two writes: the durable ## Lesson Note, then the disposition.
        assert len(say_calls) == 2
        assert "## Lesson" in say_calls[0]["body"]
        assert "CandidateDisposition: promoted" in say_calls[1]["body"]

    def test_hosted_candidate_not_in_thread_is_reported(self):
        # Candidate id absent from the loaded entries → clear not-found, not the
        # old misleading "baseline graph not found" filesystem error.
        entries = [_thread_entry("01OTHER000000000000000000A", "unrelated")]
        result, say_calls, _ = _run_hosted_promote(entries)
        assert result.startswith("❌")
        assert "not found on thread" in result
        assert "baseline graph not found" not in result
        assert say_calls == []

    def test_hosted_load_error_fails_closed(self):
        say_calls: list[dict] = []
        with (
            patch(
                "watercooler_mcp.tools.promotion._say_impl",
                side_effect=lambda **kw: say_calls.append(kw),
            ),
            patch(
                "watercooler_mcp.validation._require_context",
                return_value=(None, _hosted_context()),
            ),
            patch(
                "watercooler_mcp.hosted_ops.load_thread_entries_hosted",
                return_value=("GitHub 503", []),
            ),
        ):
            result = _promote_candidate_impl(
                candidate_entry_id=_CAND, topic=_TOPIC, target_type="Learning",
                human_authorized_by="github:caleb", ctx=MagicMock(),
                code_path="org/repo", agent_func="x:y:z",
            )
        assert result.startswith("❌")
        assert "GitHub 503" in result
        assert "refused" in result
        assert say_calls == []  # nothing written when the guard read fails
