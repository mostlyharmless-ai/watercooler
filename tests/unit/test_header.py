from __future__ import annotations

from watercooler.header import _header_split, _replace_header_line, bump_header


def test_header_split_and_replace():
    text = "Status: open\nBall: codex\n\n# Title\nBody"
    head, body = _header_split(text)
    assert "Status:" in head and body.startswith("# Title")
    new = _replace_header_line(head, "Status", "in-progress")
    assert "in-progress" in new


def test_bump_header():
    text = "Status: open\nBall: codex\n\nBody"
    out = bump_header(text, status="done", ball="claude")
    assert "Status: done" in out
    assert "Ball: claude" in out

