"""Tests for schema validation against canonical JSON schemas.

These tests ensure that watercooler-cloud data structures conform to the
canonical JSON schemas defined in schemas/.
"""

import pytest
from watercooler.schema_validation import (
    validate_thread_entry,
    validate_watercooler_thread,
    is_jsonschema_available,
)


# Skip all tests if jsonschema is not installed
pytestmark = pytest.mark.skipif(
    not is_jsonschema_available(),
    reason="jsonschema package not installed (install with: pip install jsonschema)"
)


def test_valid_thread_entry():
    """Test that a valid ThreadEntry passes validation."""
    entry = {
        "index": 0,
        "header": "Entry: Claude (user) 2025-01-26T12:00:00Z\nRole: implementer\nType: Note\nTitle: Test Entry",
        "body": "This is a test entry body.",
        "agent": "Claude (user)",
        "timestamp": "2025-01-26T12:00:00Z",
        "role": "implementer",
        "entry_type": "Note",
        "title": "Test Entry",
        "entry_id": "01HKJM2NQR8XVZWF9PQRS3T4AB",
        "start_line": 10,
        "end_line": 20,
        "start_offset": 100,
        "end_offset": 200,
    }

    is_valid, errors = validate_thread_entry(entry)
    assert is_valid, f"Valid entry failed validation: {errors}"
    assert errors == []


def test_thread_entry_with_null_optionals():
    """Test that ThreadEntry with null optional fields passes validation."""
    entry = {
        "index": 0,
        "header": "Entry: Unknown 2025-01-26T12:00:00Z",
        "body": "Minimal entry",
        "agent": None,  # Optional field
        "timestamp": None,  # Optional field
        "role": None,  # Optional field
        "entry_type": None,  # Optional field
        "title": None,  # Optional field
        "entry_id": None,  # Optional field
        "start_line": 1,
        "end_line": 5,
        "start_offset": 0,
        "end_offset": 50,
    }

    is_valid, errors = validate_thread_entry(entry)
    assert is_valid, f"Entry with null optionals failed validation: {errors}"
    assert errors == []


def test_thread_entry_missing_required_fields():
    """Test that ThreadEntry missing required fields fails validation."""
    entry = {
        "body": "Missing required fields",
        # Missing: index, header, start_line, end_line, start_offset, end_offset
    }

    is_valid, errors = validate_thread_entry(entry)
    assert not is_valid, "Entry with missing required fields should fail validation"
    assert len(errors) > 0


def test_thread_entry_invalid_entry_type():
    """Test that ThreadEntry with invalid entry_type fails validation."""
    entry = {
        "index": 0,
        "header": "Entry header",
        "body": "Entry body",
        "agent": "Claude (user)",
        "timestamp": "2025-01-26T12:00:00Z",
        "role": "implementer",
        "entry_type": "InvalidType",  # Not in enum
        "title": "Test",
        "entry_id": None,
        "start_line": 1,
        "end_line": 5,
        "start_offset": 0,
        "end_offset": 50,
    }

    is_valid, errors = validate_thread_entry(entry)
    assert not is_valid, "Entry with invalid entry_type should fail validation"
    assert len(errors) > 0


def test_valid_watercooler_thread():
    """Test that a valid WatercoolerThread passes validation."""
    thread = {
        "id": "feature-auth",
        "title": "Authentication Feature",
        "status": "OPEN",
        "ball": "Claude (user)",
        "topic": "feature-auth",
        "created": "2025-01-26T12:00:00Z",
        "priority": "P2",
        "original_topic": None,
        "entries": [
            {
                "index": 0,
                "header": "Entry: Claude (user) 2025-01-26T12:00:00Z",
                "body": "Starting work on auth feature",
                "agent": "Claude (user)",
                "timestamp": "2025-01-26T12:00:00Z",
                "role": "implementer",
                "entry_type": "Note",
                "title": "Start",
                "entry_id": "01HKJM2NQR8XVZWF9PQRS3T4AB",
                "start_line": 10,
                "end_line": 15,
                "start_offset": 100,
                "end_offset": 150,
            }
        ],
    }

    is_valid, errors = validate_watercooler_thread(thread)
    assert is_valid, f"Valid thread failed validation: {errors}"
    assert errors == []


def test_watercooler_thread_with_empty_entries():
    """Test that WatercoolerThread with empty entries passes validation."""
    thread = {
        "id": "test-thread",
        "title": "Test Thread",
        "status": "OPEN",
        "ball": "Unknown",
        "topic": "test-thread",
        "created": "2025-01-26T12:00:00Z",
        "priority": None,  # Optional
        "original_topic": None,  # Optional
        "entries": [],
    }

    is_valid, errors = validate_watercooler_thread(thread)
    assert is_valid, f"Thread with empty entries failed validation: {errors}"
    assert errors == []


def test_watercooler_thread_missing_required_fields():
    """Test that WatercoolerThread missing required fields fails validation."""
    thread = {
        "title": "Missing Required Fields",
        # Missing: id, status, ball, topic, created, entries
    }

    is_valid, errors = validate_watercooler_thread(thread)
    assert not is_valid, "Thread with missing required fields should fail validation"
    assert len(errors) > 0


def test_watercooler_thread_invalid_priority():
    """Test that WatercoolerThread with invalid priority fails validation."""
    thread = {
        "id": "test-thread",
        "title": "Test Thread",
        "status": "OPEN",
        "ball": "Unknown",
        "topic": "test-thread",
        "created": "2025-01-26T12:00:00Z",
        "priority": "INVALID",  # Not in P0-P5 or null
        "original_topic": None,
        "entries": [],
    }

    is_valid, errors = validate_watercooler_thread(thread)
    assert not is_valid, "Thread with invalid priority should fail validation"
    assert len(errors) > 0


def test_entry_id_format():
    """Test that entry_id must be 26-character ULID format."""
    # Valid ULID
    valid_entry = {
        "index": 0,
        "header": "Entry header",
        "body": "Body",
        "agent": None,
        "timestamp": None,
        "role": None,
        "entry_type": None,
        "title": None,
        "entry_id": "01HKJM2NQR8XVZWF9PQRS3T4AB",  # 26 chars, valid ULID
        "start_line": 1,
        "end_line": 5,
        "start_offset": 0,
        "end_offset": 50,
    }

    is_valid, errors = validate_thread_entry(valid_entry)
    assert is_valid, f"Valid ULID should pass: {errors}"

    # Invalid ULID (too short)
    invalid_entry = {**valid_entry, "entry_id": "TOOSHORT"}
    is_valid, errors = validate_thread_entry(invalid_entry)
    assert not is_valid, "Invalid ULID format should fail validation"
