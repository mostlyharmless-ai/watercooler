"""Schema parity test — Python EntryData ↔ JSON Schema.

Phase 4a of the build-mode authority-ladder plan (v2-build). The authority
ladder requires that schema definitions stay aligned across Python (this
repo), JSON Schema (this repo, ``src/watercooler/templates/entry_schema.json``),
and TypeScript (watercooler-site, cross-repo).

This test covers the Python ↔ JSON Schema parity. The TS ↔ JSON Schema
parity is covered by ``__tests__/schema-parity.test.ts`` in the
watercooler-site repo, which asserts the TypeScript bindings
(``lib/authorityFields.ts`` + ``interface ThreadEntry``) against a vendored
copy of this schema (``schemas/entry_schema.json``). Both tests must pass
before the Phase 4d gate is flipped to ``enforce`` (the disabled-by-default
gate code already shipped; enforcement is a runtime operator config flip).

Parity rules:

1. Every required field on EntryData (no default) MUST appear in the JSON
   Schema's ``required`` array.
2. Every optional field on EntryData (has a default) MUST appear in the
   JSON Schema's ``properties`` block (so it round-trips on read), but
   should NOT be in ``required`` (so legacy entries without it stay valid).
3. Every authority-ladder field with an enum constraint in JSON Schema
   must match the documented enum values in EntryData's docstring (the
   Python type is broadly ``Optional[str]`` — values are validated by the
   Phase 4d write check, not by the dataclass).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from watercooler.baseline_graph.writer import EntryData


SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "watercooler"
    / "templates"
    / "entry_schema.json"
)


# Field-name aliases between Python and JSON Schema. EntryData stores some
# fields that aren't directly emitted to the node (embedding lives in the
# search index, not the entry node). These are excluded from parity.
_PYTHON_ONLY_FIELDS = frozenset({"embedding"})

# JSON Schema has "id" + "type" emitted by the node builder (not part of
# EntryData itself). These are JSON-Schema-only.
_JSON_SCHEMA_ONLY_FIELDS = frozenset({"id", "type"})

# Fields that are optional on EntryData but always populated by the node
# builder (with a default value or current timestamp). The schema correctly
# requires them because the on-disk shape always carries them; the dataclass
# allows callers to omit them and let the builder fill in.
_BUILDER_ALWAYS_POPULATES = frozenset(
    {"timestamp", "summary", "file_refs", "pr_refs", "commit_refs"}
)

# Authority-ladder fields with documented enums. Pull from the
# ``watercooler.baseline_graph.writer`` constants — the **single source
# of truth** — rather than re-asserting a third hand-maintained copy
# inside this test. Per the #860 review: if a future edit changed the
# constants in writer.py without touching the schema, the test should
# catch it; the old test-local list would not.
from watercooler.baseline_graph.writer import (
    ACTOR_CLASS_VALUES,
    AUTHORITY_BASIS_VALUES,
    DECISION_ORIGIN_VALUES,
    ENTRY_TYPE_VALUES,
)

_ENUM_FIELDS = {
    "actor_class": sorted(ACTOR_CLASS_VALUES),
    "decision_origin": sorted(DECISION_ORIGIN_VALUES),
    "authority_basis": sorted(AUTHORITY_BASIS_VALUES),
    "entry_type": sorted(ENTRY_TYPE_VALUES),
}


@pytest.fixture(scope="module")
def schema() -> dict:
    with SCHEMA_PATH.open() as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def python_field_names() -> set[str]:
    return {
        f.name
        for f in dataclasses.fields(EntryData)
        if f.name not in _PYTHON_ONLY_FIELDS
    }


@pytest.fixture(scope="module")
def python_required_field_names() -> set[str]:
    return {
        f.name
        for f in dataclasses.fields(EntryData)
        if f.name not in _PYTHON_ONLY_FIELDS
        and f.default is dataclasses.MISSING
        and f.default_factory is dataclasses.MISSING
    }


class TestSchemaParity:
    def test_schema_file_exists_and_parses(self):
        assert SCHEMA_PATH.exists(), f"schema file missing at {SCHEMA_PATH}"
        with SCHEMA_PATH.open() as fh:
            data = json.load(fh)
        assert data.get("$schema", "").startswith("https://json-schema.org/")

    def test_every_python_field_appears_in_schema_properties(
        self, schema, python_field_names
    ):
        schema_properties = set(schema.get("properties", {}).keys())
        missing = python_field_names - schema_properties
        assert not missing, (
            f"Python EntryData fields missing from JSON Schema properties: "
            f"{sorted(missing)}. Add them to entry_schema.json or, if they "
            f"are intentionally Python-only, add them to _PYTHON_ONLY_FIELDS "
            f"in this test."
        )

    def test_every_python_required_field_appears_in_schema_required(
        self, schema, python_required_field_names
    ):
        schema_required = set(schema.get("required", []))
        # The JSON Schema marks id + type required too — those are node-level
        # not EntryData-level — but every Python-required field must be in
        # schema_required.
        missing = python_required_field_names - schema_required
        assert not missing, (
            f"Required Python EntryData fields missing from JSON Schema "
            f"required: {sorted(missing)}. Either give them defaults in "
            f"EntryData or add to schema required."
        )

    def test_schema_required_subset_is_node_level_plus_python_required(
        self, schema, python_required_field_names
    ):
        schema_required = set(schema.get("required", []))
        unknown = (
            schema_required
            - python_required_field_names
            - _JSON_SCHEMA_ONLY_FIELDS
            - _BUILDER_ALWAYS_POPULATES
        )
        assert not unknown, (
            f"JSON Schema requires fields that are neither Python-required, "
            f"node-level, nor builder-always-populated: {sorted(unknown)}"
        )

    def test_no_unauthorized_optional_python_field_is_marked_required(
        self, schema, python_field_names, python_required_field_names
    ):
        python_optional_field_names = (
            python_field_names - python_required_field_names - _BUILDER_ALWAYS_POPULATES
        )
        schema_required = set(schema.get("required", []))
        wrongly_required = python_optional_field_names & schema_required
        assert not wrongly_required, (
            f"JSON Schema requires fields that are optional on EntryData "
            f"and not in _BUILDER_ALWAYS_POPULATES: {sorted(wrongly_required)}. "
            f"Legacy entries without these fields would fail validation."
        )

    @pytest.mark.parametrize("field_name,expected_enum", _ENUM_FIELDS.items())
    def test_enum_values_documented_in_schema(
        self, schema, field_name, expected_enum
    ):
        prop = schema.get("properties", {}).get(field_name)
        assert prop is not None, (
            f"field {field_name!r} missing from JSON Schema properties"
        )
        actual_enum = prop.get("enum")
        assert actual_enum is not None, (
            f"field {field_name!r} should declare an enum constraint"
        )
        assert set(actual_enum) == set(expected_enum), (
            f"enum drift on {field_name!r}: schema has {sorted(actual_enum)}, "
            f"test expects {sorted(expected_enum)}"
        )

    def test_authority_fields_round_trip_through_node_builder(self, tmp_path):
        """Smoke test: setting authority fields on EntryData produces a node
        dict that includes them. None values are omitted, matching the schema
        contract that legacy / unauthorised writes produce identical shapes.
        """
        from watercooler.baseline_graph.writer import _build_entry_node

        data_with_authority = EntryData(
            entry_id="01HZA8T0BC3D4E5F6G7H8J9K0M",
            thread_topic="test",
            index=0,
            agent="ExtractDecisionsDaemon",
            role="implementer",
            entry_type="Decision",
            title="Test Decision",
            body="Spec: decision-extractor\n\n## Decision\nX",
            actor_class="daemon",
            decision_origin="daemon_extraction",
            authority_source="01HZA8T1BC3D4E5F6G7H8J9K0M",
            authority_basis="none",
            source_entry_id="01HZA8T2BC3D4E5F6G7H8J9K0M",
            human_authorized_by="github:octocat",
            confidence=5,
            gate_results={"g1_commitment": {"passed": True, "reason": "ok"}},
        )
        node = _build_entry_node(data_with_authority)

        assert node["actor_class"] == "daemon"
        assert node["decision_origin"] == "daemon_extraction"
        assert node["authority_source"] == "01HZA8T1BC3D4E5F6G7H8J9K0M"
        assert node["authority_basis"] == "none"
        assert node["source_entry_id"] == "01HZA8T2BC3D4E5F6G7H8J9K0M"
        assert node["human_authorized_by"] == "github:octocat"
        assert node["confidence"] == 5
        assert node["gate_results"] == {
            "g1_commitment": {"passed": True, "reason": "ok"}
        }

    def test_authority_fields_omitted_when_none(self):
        """Legacy / ordinary writes: None values must not appear in node."""
        from watercooler.baseline_graph.writer import _build_entry_node

        data_no_authority = EntryData(
            entry_id="01HZA8T0BC3D4E5F6G7H8J9K0M",
            thread_topic="test",
            index=0,
            agent="caleb",
            role="implementer",
            entry_type="Note",
            title="Hello",
            body="Spec: implementer\n\nHello.",
        )
        node = _build_entry_node(data_no_authority)

        for field_name in (
            "actor_class",
            "decision_origin",
            "authority_source",
            "authority_basis",
            "source_entry_id",
            "human_authorized_by",
            "confidence",
            "gate_results",
        ):
            assert field_name not in node, (
                f"field {field_name!r} should be omitted when its value is None"
            )
