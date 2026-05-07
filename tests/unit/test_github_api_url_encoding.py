"""Unit tests for the Contents-API path encoder in ``github_api``.

The encoder protects the GitHub client from a class of opaque crashes:
when a caller submits a path with characters that are illegal in a URL
(most commonly a space — e.g. a thread topic that bypassed the
dashboard's kebab-case slugify), ``urllib.request`` would reject the
constructed URL with ``URL can't contain control characters``. The fix
is a percent-encoding pass at the boundary that preserves segment
separators.
"""

from watercooler_mcp.github_api import _encode_contents_path


def test_kebab_case_path_is_unchanged() -> None:
    # The well-behaved input shape — kebab-case slugs joined by ``/``.
    # No characters need encoding, so the helper is a no-op for the
    # everyday case.
    assert (
        _encode_contents_path("graph/baseline/threads/feature-auth/meta.json")
        == "graph/baseline/threads/feature-auth/meta.json"
    )


def test_segment_separators_preserved() -> None:
    # ``/`` between segments must NOT be encoded — otherwise the GitHub
    # endpoint receives a single literal segment instead of a path.
    encoded = _encode_contents_path("a/b/c")
    assert encoded == "a/b/c"


def test_space_in_segment_is_percent_encoded() -> None:
    # The actual incident: a topic ``"test new thread"`` produced
    # ``graph/baseline/threads/test new thread/meta.json`` and the URL
    # parser rejected the literal space. Encoding turns the space into
    # ``%20`` while leaving the surrounding ``/`` untouched.
    assert (
        _encode_contents_path("graph/baseline/threads/test new thread/meta.json")
        == "graph/baseline/threads/test%20new%20thread/meta.json"
    )


def test_other_url_unsafe_characters_are_encoded() -> None:
    # ``?``, ``#`` and ``&`` would otherwise be parsed as query/fragment
    # delimiters by GitHub's router; ``%`` itself must be encoded so a
    # literal percent in a filename round-trips correctly.
    assert _encode_contents_path("dir/a?b") == "dir/a%3Fb"
    assert _encode_contents_path("dir/a#b") == "dir/a%23b"
    assert _encode_contents_path("dir/a&b") == "dir/a%26b"
    assert _encode_contents_path("dir/100%done.md") == "dir/100%25done.md"


def test_unicode_segment_is_encoded() -> None:
    # Non-ASCII characters are not "control characters" per Python's
    # ``urllib.request`` check, but they're still illegal in a URL
    # without percent-encoding. UTF-8-encode then percent-encode the
    # bytes so GitHub receives a well-formed URL it can decode back to
    # the intended filename.
    assert _encode_contents_path("dir/café.md") == "dir/caf%C3%A9.md"


def test_empty_path_is_empty() -> None:
    # ``list_files("")`` is a legitimate caller — listing repo root.
    # The encoder must not invent characters.
    assert _encode_contents_path("") == ""
