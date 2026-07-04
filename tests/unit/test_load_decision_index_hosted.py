"""Unit tests for ``load_decision_index_hosted`` (PR2 hosted reader).

The reader maps GitHub outcomes to the dispatcher contract:
- 404 (absent index) -> ``(None, None)`` (fallback signal, NOT an error),
- success -> ``(None, [records])`` (malformed lines skipped),
- API/client failure -> ``(error, None)``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from watercooler_mcp.github_api import GitHubAPIError, GitHubNotFoundError
from watercooler_mcp.hosted_ops import load_decision_index_hosted


def _patch_client(get_file):
    client = MagicMock()
    client.get_file = get_file
    return patch(
        "watercooler_mcp.hosted_ops._get_github_client", return_value=(None, client)
    )


def test_absent_index_returns_none_none():
    with _patch_client(MagicMock(side_effect=GitHubNotFoundError("404"))):
        err, records = load_decision_index_hosted()
    assert err is None
    assert records is None  # absent -> fallback, not an error


def test_success_parses_jsonl():
    blob = MagicMock()
    blob.content = '{"entry_id": "D1", "topic": "t"}\n\n{"entry_id": "D2", "topic": "t"}\n'
    with _patch_client(MagicMock(return_value=blob)):
        err, records = load_decision_index_hosted()
    assert err is None
    assert [r["entry_id"] for r in records] == ["D1", "D2"]


def test_malformed_line_skipped():
    blob = MagicMock()
    blob.content = '{"entry_id": "D1"}\nnot json\n{"entry_id": "D2"}\n'
    with _patch_client(MagicMock(return_value=blob)):
        err, records = load_decision_index_hosted()
    assert err is None
    assert [r["entry_id"] for r in records] == ["D1", "D2"]


def test_non_dict_json_line_skipped():
    # Valid JSON but not an object — must be dropped, not handed to the reader
    # (which would crash on rec.get(...)). Mirrors load_entries_hosted.
    blob = MagicMock()
    blob.content = '{"entry_id": "D1"}\n[]\n"bad"\n42\n{"entry_id": "D2"}\n'
    with _patch_client(MagicMock(return_value=blob)):
        err, records = load_decision_index_hosted()
    assert err is None
    assert [r["entry_id"] for r in records] == ["D1", "D2"]
    assert all(isinstance(r, dict) for r in records)


def test_api_error_returns_error():
    with _patch_client(MagicMock(side_effect=GitHubAPIError("boom"))):
        err, records = load_decision_index_hosted()
    assert err is not None
    assert records is None


def test_no_client_returns_error():
    with patch(
        "watercooler_mcp.hosted_ops._get_github_client",
        return_value=("no token", None),
    ):
        err, records = load_decision_index_hosted()
    assert err == "no token"
    assert records is None


# ---------------------------------------------------------------------------
# build_decision_index_hosted (PR4 hosted backfill)
# ---------------------------------------------------------------------------

_SRC = "SRC0000000000000000000000"
_DEC = "DEC0000000000000000000000"


def test_build_decision_index_hosted_writes_records():
    from watercooler_mcp import hosted_ops

    entries = {
        "feat-b": [
            {"id": f"entry:{_SRC}", "entry_type": "Note", "title": "s", "timestamp": "t"},
            {
                "id": f"entry:{_DEC}",
                "entry_type": "Decision",
                "title": "d",
                "body": "Confidence: 4/5",
                "timestamp": "t",
                "agent": "a",
                "role": "planner",
            },
        ]
    }
    states = {
        "feat-b": (
            None,
            {
                "annotation_states": {
                    _DEC: {"xrefs": [_SRC], "tags": []},
                    _SRC: {"xrefs": [_DEC], "tags": ["decision_extracted"]},
                }
            },
        )
    }
    put = MagicMock()
    client = MagicMock()
    client.put_file = put
    client.get_file = MagicMock(side_effect=GitHubNotFoundError("404"))

    with (
        patch.object(hosted_ops, "load_all_entries_hosted", return_value=(None, entries)),
        patch.object(
            hosted_ops,
            "get_annotations_hosted",
            side_effect=lambda topic, target_id="": states[topic],
        ),
        patch.object(hosted_ops, "_get_github_client", return_value=(None, client)),
    ):
        err, count = hosted_ops.build_decision_index_hosted()

    assert err is None
    assert count == 1  # only the Decision is indexed
    put.assert_called_once()
    kwargs = put.call_args.kwargs
    assert kwargs["path"].endswith("graph/baseline/decisions-index.jsonl")
    assert kwargs["sha"] is None  # new file (get_file 404)
    assert _DEC in kwargs["content"]
    assert "decision_extracted" not in kwargs["content"]  # tags not indexed


def test_build_decision_index_hosted_load_error_propagates():
    from watercooler_mcp import hosted_ops

    with patch.object(
        hosted_ops, "load_all_entries_hosted", return_value=("boom", {})
    ):
        err, count = hosted_ops.build_decision_index_hosted()
    assert err == "boom"
    assert count == 0


def _dec_entry(body=""):
    return {
        "id": f"entry:{_DEC}",
        "entry_id": _DEC,
        "entry_type": "Decision",
        "title": "d",
        "body": body,
        "timestamp": "t",
        "agent": "a",
        "role": "planner",
    }


def test_extra_for_say_decision_upserts_new_file():
    from watercooler_mcp import hosted_ops

    client = MagicMock()
    client.get_file = MagicMock(side_effect=GitHubNotFoundError("404"))  # index absent
    with patch.object(
        hosted_ops,
        "get_annotations_hosted",
        return_value=(None, {"annotation_states": {}}),
    ):
        files, shas = hosted_ops._decision_index_extra_for_say(
            client, "feat-b", [_dec_entry("Confidence: 2/5")], _DEC, "Decision"
        )
    assert len(files) == 1
    path, content = files[0]
    assert path.endswith("graph/baseline/decisions-index.jsonl")
    assert _DEC in content
    assert shas[path] is None  # new file


def test_extra_for_say_non_decision_is_empty():
    from watercooler_mcp import hosted_ops

    files, shas = hosted_ops._decision_index_extra_for_say(
        MagicMock(), "feat-b", [], "X", "Note"
    )
    assert files == [] and shas == {}


def test_extra_for_say_preserves_existing_cross_thread_source():
    import json as _json

    from watercooler_mcp import hosted_ops

    existing = (
        _json.dumps(
            {
                "entry_id": _DEC,
                "topic": "feat-b",
                "source": {"entry_id": _SRC, "topic": "other", "title": "s", "timestamp": "t"},
                "extracted": True,
                "confidence": 4,
            }
        )
        + "\n"
    )
    blob = MagicMock()
    blob.content = existing
    blob.sha = "sha123"
    client = MagicMock()
    client.get_file = MagicMock(return_value=blob)

    with patch.object(
        hosted_ops,
        "get_annotations_hosted",
        return_value=(None, {"annotation_states": {}}),  # no same-thread xref now
    ):
        files, shas = hosted_ops._decision_index_extra_for_say(
            client, "feat-b", [_dec_entry()], _DEC, "Decision"
        )

    rec = _json.loads(files[0][1].strip())
    assert rec["source"]["topic"] == "other"  # not clobbered to None
    assert rec["extracted"] is True
    assert shas[files[0][0]] == "sha123"  # conflict-check SHA threaded through


def test_extra_for_delete_decision_prunes():
    import json as _json

    from watercooler_mcp import hosted_ops

    existing = (
        _json.dumps({"entry_id": _DEC, "topic": "feat-b"})
        + "\n"
        + _json.dumps({"entry_id": "OTHER", "topic": "t"})
        + "\n"
    )
    blob = MagicMock()
    blob.content = existing
    blob.sha = "s"
    client = MagicMock()
    client.get_file = MagicMock(return_value=blob)

    files, shas = hosted_ops._decision_index_extra_for_delete(client, "Decision", _DEC)
    content = files[0][1]
    assert _DEC not in content
    assert "OTHER" in content
    assert shas[files[0][0]] == "s"


def test_extra_for_delete_non_decision_is_empty():
    from watercooler_mcp import hosted_ops

    files, shas = hosted_ops._decision_index_extra_for_delete(MagicMock(), "Note", "X")
    assert files == [] and shas == {}


def test_build_decision_index_hosted_annotation_error_fails_no_write():
    # A real annotation read error must fail the rebuild rather than publish a
    # degraded index (review #1008). 404 (None, empty) is fine; ("boom", {}) is not.
    from watercooler_mcp import hosted_ops

    entries = {
        "feat-b": [
            {
                "id": f"entry:{_DEC}",
                "entry_type": "Decision",
                "title": "d",
                "body": "Confidence: 4/5",
                "timestamp": "t",
            }
        ]
    }
    put = MagicMock()
    client = MagicMock()
    client.put_file = put

    with (
        patch.object(hosted_ops, "load_all_entries_hosted", return_value=(None, entries)),
        patch.object(
            hosted_ops, "get_annotations_hosted", return_value=("boom", {})
        ),
        patch.object(hosted_ops, "_get_github_client", return_value=(None, client)),
    ):
        err, count = hosted_ops.build_decision_index_hosted()

    assert err is not None
    assert "boom" in err
    assert count == 0
    put.assert_not_called()  # no degraded index published
