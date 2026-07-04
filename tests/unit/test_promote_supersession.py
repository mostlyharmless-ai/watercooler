"""PR2: watercooler_promote_candidate(target_type="Supersession") ratifies an earned edge.

The governed L3 ratification (RFC P3): a human promotes a daemon-emitted earned_edge
candidate through the SAME guards as Decision/Learning promotion (authorizer scrub,
needs_human_confirmation, append-only double-promotion), which then writes the append-only
xref_supersedes annotation (superseded → successor) + a CandidateDisposition Note.
"""

import json
from unittest.mock import patch

from watercooler.promotion import parse_candidate_body
import watercooler_mcp.tools.promotion as p

_OK = json.dumps({"status": "ok", "event_id": "01EV", "annotation_state": {}})
_SAY = "Entry-ID: 01DISP\nok"

_CANDIDATE = (
    "Spec: general-purpose\n"
    "Promotion-Source: earned_edge\n"
    "Superseded-Entry: 01A\n"
    "Superseded-By-Entry: 01B\n"
    "Basis: temporal_only\n"
    "Authority: none\n"
    "Candidate-Status: needs_human_confirmation\n\n"
    "Entry 01A appears superseded by entry 01B.\n"
)


def _call(body=_CANDIDATE, authorizer="caleb", existing=None):
    meta = parse_candidate_body(body, "01KWE6YNKW1S7Y3XCC26PPHS88", "topic-a")
    return p._promote_supersession_candidate(
        meta=meta,
        candidate_entry_id="01KWE6YNKW1S7Y3XCC26PPHS88",
        topic="topic-a",
        candidate_body=body,
        human_authorized_by=authorizer,
        existing_thread_entries=existing or [],
        ctx=None,
        code_path="/repo",
        agent_func="Claude Code:x:implementer",
    )


def test_happy_path_appends_xref_supersedes_and_disposition():
    with patch("watercooler_mcp.tools.graph._annotate_impl", return_value=_OK) as ann, patch.object(
        p, "_say_impl", return_value=_SAY
    ) as say:
        out = _call()
    args = ann.call_args[0]
    assert args[1] == "01A" and args[2] == "entry"
    assert args[3] == "xref_supersedes" and args[4] == "01B"
    # disposition carries the guard key so re-promotion is blocked next time
    disp_body = say.call_args.kwargs["body"]
    assert "Disposition-Target: 01KWE6YNKW1S7Y3XCC26PPHS88" in disp_body and "CandidateDisposition: promoted" in disp_body
    assert "01A → 01B" in out


def test_rejects_empty_authorizer_before_any_write():
    with patch("watercooler_mcp.tools.graph._annotate_impl") as ann, patch.object(
        p, "_say_impl"
    ) as say:
        out = _call(authorizer="   ")
    assert out.startswith("❌")
    ann.assert_not_called()
    say.assert_not_called()


def test_rejects_already_dispositioned_candidate():
    prior = [{
        "entry_id": "01D", "entry_type": "Note", "title": "disp",
        "body": "CandidateDisposition: promoted\nDisposition-Target: 01KWE6YNKW1S7Y3XCC26PPHS88\n",
    }]
    with patch("watercooler_mcp.tools.graph._annotate_impl") as ann, patch.object(
        p, "_say_impl"
    ) as say:
        out = _call(existing=prior)
    assert out.startswith("❌")
    ann.assert_not_called()  # double-promotion guard fires before the xref write
    say.assert_not_called()


def test_missing_contract_lines_errors():
    body = _CANDIDATE.replace("Superseded-By-Entry: 01B\n", "")
    with patch.object(p, "_say_impl", return_value=_SAY) as say:
        out = _call(body=body)
    assert out.startswith("❌") and "Superseded" in out
    say.assert_not_called()


def test_xref_append_failure_errors_and_writes_no_disposition():
    with patch(
        "watercooler_mcp.tools.graph._annotate_impl",
        return_value="Error adding annotation (hosted): 502",
    ), patch.object(p, "_say_impl", return_value=_SAY) as say:
        out = _call()
    assert out.startswith("❌") and "xref_supersedes append failed" in out
    say.assert_not_called()


def test_rejects_note_missing_candidate_status():
    """An arbitrary Note with the two markers but no candidate state is NOT ratifiable."""
    body = _CANDIDATE.replace("Candidate-Status: needs_human_confirmation\n", "")
    with patch("watercooler_mcp.tools.graph._annotate_impl") as ann, patch.object(p, "_say_impl") as say:
        out = _call(body=body)
    assert out.startswith("❌") and "needs_human_confirmation" in out
    ann.assert_not_called()
    say.assert_not_called()


def test_rejects_note_missing_earned_edge_source():
    body = _CANDIDATE.replace("Promotion-Source: earned_edge\n", "")
    with patch("watercooler_mcp.tools.graph._annotate_impl") as ann, patch.object(p, "_say_impl") as say:
        out = _call(body=body)
    assert out.startswith("❌") and "earned_edge" in out
    ann.assert_not_called()
    say.assert_not_called()
