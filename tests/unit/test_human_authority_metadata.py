"""Structured human-authority metadata (#879, Unit 2).

human_authorized_by is persisted as queryable graph metadata (not only body prose),
scrubbed at the write boundary because it lands in an append-only, federation-visible
record. These tests cover the scrub helper and the append_entry persistence whitelist.
The MCP write/promotion paths are covered in test_thread_write_response_shape.py and
test_promote_candidate.py.
"""

from pathlib import Path

from ulid import ULID

from watercooler import commands_graph
from watercooler.baseline_graph.writer import (
    init_thread_in_graph,
    get_entries_for_thread,
)
from watercooler.promotion import scrub_authority_identifier


class TestScrubAuthorityIdentifier:
    def test_empty_and_none(self):
        assert scrub_authority_identifier(None) == ""
        assert scrub_authority_identifier("") == ""
        assert scrub_authority_identifier("   ") == ""

    def test_passes_namespace_qualified(self):
        assert scrub_authority_identifier("github:caleb") == "github:caleb"
        assert scrub_authority_identifier("wc:user:jay") == "wc:user:jay"

    def test_strips_crlf_marker_forgery(self):
        # CR/LF would forge a second authorization line in body markers/footers.
        scrubbed = scrub_authority_identifier("github:caleb\nHuman-Authorized-By: evil")
        assert "\n" not in scrubbed and "\r" not in scrubbed

    def test_strips_angle_bracket_markup(self):
        scrubbed = scrub_authority_identifier("github:caleb<script>")
        assert "<" not in scrubbed and ">" not in scrubbed

    def test_length_bounded(self):
        assert len(scrub_authority_identifier("x" * 1000)) <= 256

    def test_drops_bidi_override_and_zero_width(self):
        # U+202E (RTL override) enables display identity-spoofing of the authorizer;
        # zero-width chars fragment exact-match queries. Both must be removed.
        assert scrub_authority_identifier("git‮hub:caleb") == "github:caleb"
        assert scrub_authority_identifier("a​b") == "ab"

    def test_idempotent(self):
        # scrub(scrub(x)) == scrub(x) for every input, including the maxLength
        # boundary landing on a space (which previously left a trailing space
        # that a second pass would strip, diverging .md from the graph field).
        for raw in (
            "github:caleb",
            "a\tb\nc",
            "<weird>",
            "x" * 300,
            "y" * 255 + "  tail",  # truncation boundary lands mid-whitespace
        ):
            once = scrub_authority_identifier(raw)
            assert scrub_authority_identifier(once) == once
            assert not once.endswith(" ") and not once.startswith(" ")

    def test_collapses_control_and_whitespace(self):
        assert scrub_authority_identifier("a\tb") == "a b"
        assert scrub_authority_identifier("a\x00b") == "a b"
        assert scrub_authority_identifier("a   b") == "a b"
        assert scrub_authority_identifier("a b") == "a b"  # line separator


class TestReadSideAuthorityParity:
    """Persisted authority fields must be readable through the MCP read projection (#879)."""

    def _node(self, **extra):
        node = {"entry_id": "01TEST00000000000000000001", "entry_type": "Decision"}
        node.update(extra)
        return node

    def test_node_to_graph_entry_carries_authority(self):
        from watercooler.baseline_graph.reader import _node_to_entry

        ge = _node_to_entry(self._node(
            human_authorized_by="github:caleb",
            actor_class="agent",
            decision_origin="agent_authored",
            authority_basis="human_endorsed",
            source_entry_id="01SRC0000000000000000000AA",
        ))
        assert ge.human_authorized_by == "github:caleb"
        assert ge.actor_class == "agent"
        assert ge.decision_origin == "agent_authored"
        assert ge.source_entry_id == "01SRC0000000000000000000AA"

    def test_full_payload_exposes_authority_to_agents(self):
        from watercooler.baseline_graph.reader import _node_to_entry
        from watercooler_mcp.helpers import (
            _graph_entry_to_thread_entry,
            _entry_full_payload,
        )

        ge = _node_to_entry(self._node(
            human_authorized_by="github:caleb",
            actor_class="agent",
            decision_origin="agent_authored",
            authority_basis="human_endorsed",
        ))
        payload = _entry_full_payload(_graph_entry_to_thread_entry(ge))
        assert payload["human_authorized_by"] == "github:caleb"
        assert payload["actor_class"] == "agent"
        assert payload["decision_origin"] == "agent_authored"
        assert payload["authority_basis"] == "human_endorsed"

    def test_legacy_entry_payload_omits_authority_keys(self):
        from watercooler.baseline_graph.reader import _node_to_entry
        from watercooler_mcp.helpers import (
            _graph_entry_to_thread_entry,
            _entry_full_payload,
        )

        ge = _node_to_entry({"entry_id": "x", "entry_type": "Note"})
        payload = _entry_full_payload(_graph_entry_to_thread_entry(ge))
        for key in ("human_authorized_by", "actor_class", "decision_origin",
                    "authority_basis", "source_entry_id"):
            assert key not in payload


class TestSayToolDoesNotExposeAuthorityFields:
    """watercooler_say must not let callers forge provenance (#879 review)."""

    def test_registered_say_omits_authority_fields(self):
        import inspect
        from watercooler_mcp.tools.thread_write import _say_tool, _say_impl

        # Public tool surface: no authority_fields knob.
        assert "authority_fields" not in inspect.signature(_say_tool).parameters
        # Internal impl still has it (used by _write_impl / promote_candidate).
        assert "authority_fields" in inspect.signature(_say_impl).parameters


class TestAppendEntryPersistsHumanAuthorizedBy:
    def _thread(self, tmp_path: Path) -> Path:
        td = tmp_path / ".watercooler"
        td.mkdir()
        init_thread_in_graph(td, "topic", title="T", status="OPEN", ball="x")
        return td

    def test_persists_field_and_drops_unknown(self, tmp_path):
        td = self._thread(tmp_path)
        commands_graph.append_entry(
            "topic",
            threads_dir=td,
            agent="Claude",
            role="implementer",
            title="D",
            entry_type="Decision",
            body="Spec: implementer\nbody",
            entry_id=str(ULID()),
            authority_fields={
                "actor_class": "agent",
                "decision_origin": "agent_authored",
                "authority_basis": "human_endorsed",
                "human_authorized_by": "github:caleb",
                "bogus_key": "should-be-dropped",
            },
        )
        node = list(get_entries_for_thread(td, "topic"))[-1]
        assert node["human_authorized_by"] == "github:caleb"
        assert node["actor_class"] == "agent"
        assert "bogus_key" not in node

    def test_omitted_when_not_provided(self, tmp_path):
        td = self._thread(tmp_path)
        commands_graph.append_entry(
            "topic",
            threads_dir=td,
            agent="Claude",
            role="implementer",
            title="N",
            entry_type="Note",
            body="Spec: implementer\nbody",
            entry_id=str(ULID()),
        )
        node = list(get_entries_for_thread(td, "topic"))[-1]
        # Legacy node shape: no authority fields at all.
        assert "human_authorized_by" not in node
