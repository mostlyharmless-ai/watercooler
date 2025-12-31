from __future__ import annotations

from pathlib import Path
from watercooler.templates import _fill_template


def test_fill_template_double_brace():
    """Test {{KEY}} placeholder format."""
    template = "Hello {{NAME}}, welcome to {{PLACE}}!"
    mapping = {"NAME": "Alice", "PLACE": "Wonderland"}
    result = _fill_template(template, mapping)
    assert result == "Hello Alice, welcome to Wonderland!"


def test_fill_template_angle_bracket():
    """Test <KEY> placeholder format."""
    template = "Hello <NAME>, welcome to <PLACE>!"
    mapping = {"NAME": "Bob", "PLACE": "NYC"}
    result = _fill_template(template, mapping)
    assert result == "Hello Bob, welcome to NYC!"


def test_fill_template_mixed_formats():
    """Test both {{KEY}} and <KEY> in same template."""
    template = "Agent: {{AGENT}}, Role: <ROLE>"
    mapping = {"AGENT": "Claude", "ROLE": "critic"}
    result = _fill_template(template, mapping)
    assert result == "Agent: Claude, Role: critic"


def test_fill_template_special_utc():
    """Test special <YYYY-MM-DDTHH:MM:SSZ> placeholder."""
    template = "Created: <YYYY-MM-DDTHH:MM:SSZ>"
    mapping = {"UTC": "2025-10-06T12:00:00Z"}
    result = _fill_template(template, mapping)
    assert result == "Created: 2025-10-06T12:00:00Z"


def test_fill_template_special_agent():
    """Test special <Codex|Claude|Team> placeholder."""
    template = "By: <Codex|Claude|Team>"
    mapping = {"AGENT": "Codex"}
    result = _fill_template(template, mapping)
    assert result == "By: Codex"


def test_fill_template_ball_special():
    """Test special Ball: <Codex|Claude|Team> placeholder."""
    template = "Ball: <Codex|Claude|Team>"
    mapping = {"BALL": "Claude"}
    result = _fill_template(template, mapping)
    assert result == "Ball: Claude"


def test_fill_template_topic_special():
    """Test special Topic: <Short title> placeholder."""
    template = "Topic: <Short title>"
    mapping = {"Short title": "Feature Implementation"}
    result = _fill_template(template, mapping)
    assert result == "Topic: Feature Implementation"


def test_fill_template_missing_key():
    """Test that missing keys are left as-is."""
    template = "Hello {{NAME}}, from {{CITY}}"
    mapping = {"NAME": "Charlie"}
    result = _fill_template(template, mapping)
    assert result == "Hello Charlie, from {{CITY}}"


def test_fill_template_empty_mapping():
    """Test with empty mapping."""
    template = "Static content only"
    mapping = {}
    result = _fill_template(template, mapping)
    assert result == "Static content only"


def test_fill_template_entry_block():
    """Test realistic entry block template."""
    template = """---
Entry: {{AGENT}} {{UTC}}
Type: {{TYPE}}
Title: {{TITLE}}

{{BODY}}"""
    mapping = {
        "AGENT": "Claude (agent)",
        "UTC": "2025-10-06T12:00:00Z",
        "TYPE": "Decision",
        "TITLE": "Approve Implementation",
        "BODY": "All tests passing, ready to merge."
    }
    result = _fill_template(template, mapping)
    assert "Entry: Claude (agent) 2025-10-06T12:00:00Z" in result
    assert "Type: Decision" in result
    assert "Title: Approve Implementation" in result
    assert "All tests passing" in result
