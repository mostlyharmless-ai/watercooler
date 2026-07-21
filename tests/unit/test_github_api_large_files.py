"""Unit tests for ``get_file``'s over-1MB blob fallback.

GitHub's Contents API won't inline blobs over 1MB: it returns HTTP 200
with ``encoding: "none"``, an empty ``content`` field, and a valid
``sha``. Before the fallback, ``get_file`` decoded that empty string and
returned an "empty file" — and because all hosted writers re-render
``entries.jsonl`` from the parsed entry list, the next write clobbered
the thread down to a single entry with a successful commit (2026-07-20
compliance-thread truncation incident). ``get_file`` must instead fetch
the blob via the Git Blobs API, and refuse to return empty content for
a file whose reported size is non-zero.
"""

import base64
from unittest.mock import patch

import pytest

from watercooler_mcp.github_api import (
    GitHubAPIError,
    GitHubClient,
    GitHubNotFoundError,
)


def _client() -> GitHubClient:
    return GitHubClient(token="t", repo="org/repo", branch="watercooler/threads")


def _contents_response(
    content: str = "", encoding: str = "base64", size: int = 0
) -> dict:
    return {
        "content": content,
        "encoding": encoding,
        "sha": "abc123",
        "path": "graph/baseline/threads/big-thread/entries.jsonl",
        "size": size,
    }


def test_small_file_is_read_inline_without_blob_call() -> None:
    # The everyday case: content is inlined, no second request.
    inline = base64.b64encode(b'{"entry_id": "01A"}\n').decode()
    with patch.object(
        GitHubClient,
        "_make_request",
        return_value=_contents_response(content=inline, size=20),
    ) as mock_req:
        result = _client().get_file("graph/baseline/threads/t/entries.jsonl")

    assert result.content == '{"entry_id": "01A"}\n'
    assert mock_req.call_count == 1


def test_over_limit_file_falls_back_to_blobs_api() -> None:
    # Contents response for a >1MB blob: 200, encoding "none", empty
    # content, valid sha. The client must follow up on the Blobs API
    # and return the full content, not an empty file.
    full = '{"entry_id": "01A"}\n{"entry_id": "01B"}\n'
    blob_b64 = base64.b64encode(full.encode()).decode()
    responses = [
        _contents_response(encoding="none", size=1_500_000),
        {"content": blob_b64, "encoding": "base64", "sha": "abc123"},
    ]
    with patch.object(
        GitHubClient, "_make_request", side_effect=responses
    ) as mock_req:
        result = _client().get_file("graph/baseline/threads/t/entries.jsonl")

    assert result.content == full
    assert result.sha == "abc123"
    blob_endpoint = mock_req.call_args_list[1].args[1]
    assert blob_endpoint == "/repos/org/repo/git/blobs/abc123"


def test_blob_content_with_newlines_is_decoded() -> None:
    # The Blobs API base64 payload is newline-wrapped like the Contents
    # API's; the existing newline-stripping must apply to it too.
    full = "x" * 100
    blob_b64 = base64.b64encode(full.encode()).decode()
    wrapped = "\n".join(blob_b64[i : i + 60] for i in range(0, len(blob_b64), 60))
    responses = [
        _contents_response(encoding="none", size=1_500_000),
        {"content": wrapped + "\n", "encoding": "base64", "sha": "abc123"},
    ]
    with patch.object(GitHubClient, "_make_request", side_effect=responses):
        result = _client().get_file("big.jsonl")

    assert result.content == full


def test_unretrievable_content_raises_instead_of_returning_empty() -> None:
    # If the blob fallback also yields no content (e.g. over the Blobs
    # API's own 100MB ceiling), returning an empty file would re-open
    # the silent-truncation window. It must raise.
    responses = [
        _contents_response(encoding="none", size=200_000_000),
        {"content": "", "encoding": "none", "sha": "abc123"},
    ]
    with patch.object(GitHubClient, "_make_request", side_effect=responses):
        with pytest.raises(GitHubAPIError, match="refusing to treat it as empty"):
            _client().get_file("huge.jsonl")


def test_blob_fallback_404_does_not_masquerade_as_missing_file() -> None:
    # The Contents response just proved the file exists; a 404 from the
    # follow-up Blobs request (endpoint/permission/transient failure)
    # must NOT escape as GitHubNotFoundError — callers like
    # _read_per_thread_graph and file_exists read NotFound as "file
    # absent", and an absent entries.jsonl is rewritten from scratch on
    # the next hosted write. It must surface as a plain GitHubAPIError.
    responses = [
        _contents_response(encoding="none", size=1_500_000),
        GitHubNotFoundError("Not found: blob", status_code=404),
    ]
    with patch.object(GitHubClient, "_make_request", side_effect=responses):
        with pytest.raises(GitHubAPIError, match="returned 404 although") as exc_info:
            _client().get_file("graph/baseline/threads/t/entries.jsonl")

    assert not isinstance(exc_info.value, GitHubNotFoundError)


def test_contents_404_still_raises_not_found() -> None:
    # The initial Contents request 404ing genuinely means the file does
    # not exist — that semantic must survive the fallback change so
    # new-thread creation keeps working.
    with patch.object(
        GitHubClient,
        "_make_request",
        side_effect=GitHubNotFoundError("Not found: file", status_code=404),
    ):
        with pytest.raises(GitHubNotFoundError):
            _client().get_file("graph/baseline/threads/new/entries.jsonl")


def test_genuinely_empty_file_still_reads_as_empty() -> None:
    # size == 0 is a legitimately empty file — no fallback, no error.
    with patch.object(
        GitHubClient,
        "_make_request",
        return_value=_contents_response(content="", size=0),
    ) as mock_req:
        result = _client().get_file("empty.jsonl")

    assert result.content == ""
    assert mock_req.call_count == 1
