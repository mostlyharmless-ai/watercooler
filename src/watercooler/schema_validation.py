"""Schema validation utilities for Watercooler data structures.

This module provides utilities for validating ThreadEntry and WatercoolerThread
objects against the canonical JSON schemas defined in schemas/.

Note: Requires jsonschema package (install with 'pip install jsonschema')
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    from jsonschema import validate, ValidationError, SchemaError, Draft7Validator
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT7
    JSONSCHEMA_AVAILABLE = True
    REFERENCING_AVAILABLE = True
except ImportError:
    try:
        from jsonschema import validate, ValidationError, SchemaError
        JSONSCHEMA_AVAILABLE = True
        REFERENCING_AVAILABLE = False
    except ImportError:
        JSONSCHEMA_AVAILABLE = False
        REFERENCING_AVAILABLE = False
        ValidationError = Exception  # type: ignore
        SchemaError = Exception  # type: ignore


def _get_schema_path(schema_name: str) -> Path:
    """Get path to a schema file.

    Args:
        schema_name: Name of schema file (e.g., 'thread_entry.schema.json')

    Returns:
        Path to schema file
    """
    # schemas/ is at repo root, one level up from src/watercooler
    repo_root = Path(__file__).parent.parent.parent
    return repo_root / "schemas" / schema_name


@lru_cache(maxsize=8)
def load_schema(schema_name: str) -> dict[str, Any]:
    """Load a JSON schema from schemas/ directory.

    Args:
        schema_name: Name of schema file (e.g., 'thread_entry.schema.json')

    Returns:
        Parsed schema as dictionary

    Raises:
        FileNotFoundError: If schema file doesn't exist
        json.JSONDecodeError: If schema is invalid JSON

    Note:
        Results are cached to avoid repeated file I/O.
    """
    schema_path = _get_schema_path(schema_name)
    if not schema_path.exists():
        raise FileNotFoundError(f"Schema not found: {schema_path}")

    return json.loads(schema_path.read_text())


@lru_cache(maxsize=1)
def _create_schema_registry() -> Any:
    """Create a referencing registry with all watercooler schemas (cached).

    This enables $ref resolution between schemas (e.g., watercooler_thread
    referencing thread_entry).

    Returns:
        A referencing.Registry with all schemas loaded, or None if
        referencing is not available.

    Note:
        Results are cached since schemas don't change at runtime.
    """
    if not REFERENCING_AVAILABLE:
        return None

    # Load both schemas
    thread_entry_schema = load_schema("thread_entry.schema.json")
    watercooler_thread_schema = load_schema("watercooler_thread.schema.json")

    # Create resources for each schema with their $id as the URI
    # The $ref in watercooler_thread.schema.json is relative ("thread_entry.schema.json")
    # so we need to map both the full URI and the relative filename
    resources = [
        # Map by full $id URI
        (
            thread_entry_schema.get("$id", "thread_entry.schema.json"),
            Resource.from_contents(thread_entry_schema, default_specification=DRAFT7),
        ),
        (
            watercooler_thread_schema.get("$id", "watercooler_thread.schema.json"),
            Resource.from_contents(watercooler_thread_schema, default_specification=DRAFT7),
        ),
        # Map by relative filename (for $ref resolution)
        (
            "thread_entry.schema.json",
            Resource.from_contents(thread_entry_schema, default_specification=DRAFT7),
        ),
        (
            "watercooler_thread.schema.json",
            Resource.from_contents(watercooler_thread_schema, default_specification=DRAFT7),
        ),
    ]

    return Registry().with_resources(resources)


def validate_thread_entry(entry_dict: dict[str, Any]) -> tuple[bool, list[str]]:
    """Validate a thread entry dictionary against canonical schema.

    Args:
        entry_dict: Dictionary representation of ThreadEntry

    Returns:
        Tuple of (is_valid, errors) where errors is list of all error messages

    Example:
        >>> entry = {
        ...     "index": 0,
        ...     "header": "Entry: Claude (user) 2025-01-26T12:00:00Z",
        ...     "body": "Entry content",
        ...     "agent": "Claude (user)",
        ...     "timestamp": "2025-01-26T12:00:00Z",
        ...     "start_line": 10,
        ...     "end_line": 20,
        ...     "start_offset": 100,
        ...     "end_offset": 200,
        ... }
        >>> is_valid, errors = validate_thread_entry(entry)
        >>> assert is_valid and not errors
    """
    if not JSONSCHEMA_AVAILABLE:
        return False, ["jsonschema package not installed. Install with: pip install jsonschema"]

    try:
        schema = load_schema("thread_entry.schema.json")
        validator = Draft7Validator(schema)
        errors_list = list(validator.iter_errors(entry_dict))
        if errors_list:
            return False, [err.message for err in errors_list]
        return True, []
    except SchemaError as e:
        return False, [f"Schema error: {e.message}"]
    except FileNotFoundError as e:
        return False, [str(e)]
    except json.JSONDecodeError as e:
        return False, [f"Invalid schema JSON: {e}"]


def validate_watercooler_thread(thread_dict: dict[str, Any]) -> tuple[bool, list[str]]:
    """Validate a thread dictionary against canonical schema.

    Args:
        thread_dict: Dictionary representation of WatercoolerThread

    Returns:
        Tuple of (is_valid, errors) where errors is list of all error messages

    Example:
        >>> thread = {
        ...     "id": "feature-auth",
        ...     "title": "Authentication Feature",
        ...     "status": "OPEN",
        ...     "ball": "Claude (user)",
        ...     "topic": "feature-auth",
        ...     "created": "2025-01-26T12:00:00Z",
        ...     "priority": "P2",
        ...     "entries": [],
        ... }
        >>> is_valid, errors = validate_watercooler_thread(thread)
        >>> assert is_valid and not errors
    """
    if not JSONSCHEMA_AVAILABLE:
        return False, ["jsonschema package not installed. Install with: pip install jsonschema"]

    try:
        schema = load_schema("watercooler_thread.schema.json")

        # Use registry for $ref resolution (watercooler_thread references thread_entry)
        registry = _create_schema_registry()
        if registry is not None:
            validator = Draft7Validator(schema, registry=registry)
            errors_list = list(validator.iter_errors(thread_dict))
            if errors_list:
                return False, [err.message for err in errors_list]
            return True, []
        else:
            # Fallback to simple validation (may fail on $ref)
            validate(instance=thread_dict, schema=schema)
            return True, []
    except ValidationError as e:
        return False, [str(e.message)]
    except SchemaError as e:
        return False, [f"Schema error: {e.message}"]
    except FileNotFoundError as e:
        return False, [str(e)]
    except json.JSONDecodeError as e:
        return False, [f"Invalid schema JSON: {e}"]


def is_jsonschema_available() -> bool:
    """Check if jsonschema package is available.

    Returns:
        True if jsonschema is installed, False otherwise
    """
    return JSONSCHEMA_AVAILABLE
