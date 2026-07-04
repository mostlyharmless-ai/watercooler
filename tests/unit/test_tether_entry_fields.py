"""#896 Leg 2 — the §6 tether read-model as structured entry-node metadata.

Covers ``WarrantReadModel.to_entry_fields`` and that ``_build_entry_node`` emits
the fields onto the node (None/empty-omitted), so the dashboard can render support
structurally instead of re-parsing the body markers (the §7 anti-pattern).
"""

from __future__ import annotations

from watercooler.authority_support import (
    TETHER_INTERPRETIVE,
    TETHER_SOURCE,
    build_read_model,
)
from watercooler.baseline_graph.writer import EntryData, _build_entry_node


def _entry(**kw) -> EntryData:
    base = dict(
        entry_id="01TESTENTRY00000000000000",
        thread_topic="t",
        index=0,
        agent="A",
        role="scribe",
        entry_type="Decision",
        title="x",
        body="b",
    )
    base.update(kw)
    return EntryData(**base)


class TestToEntryFields:
    def test_shape_and_reason_omitted_when_supported(self):
        model = build_read_model(
            [
                {"tether": TETHER_SOURCE, "label": "quote", "quote_hash": "abc"},
                {"tether": TETHER_INTERPRETIVE, "label": "gen"},
            ]
        )
        fields = model.to_entry_fields()
        assert fields["support_counts"] == {"source": 1, "interpretive": 1}
        assert fields["dominant_tether"] == "source"  # strength, not frequency
        assert fields["thin_support"] is False
        assert "thin_support_reason" not in fields  # not thin -> omitted
        assert len(fields["support_evidence"]) == 2

    def test_thin_includes_reason(self):
        model = build_read_model([{"tether": TETHER_INTERPRETIVE, "label": "gen"}])
        fields = model.to_entry_fields()
        assert fields["thin_support"] is True
        assert isinstance(fields["thin_support_reason"], str) and fields["thin_support_reason"]

    def test_empty_warrant_omits_evidence(self):
        fields = build_read_model([]).to_entry_fields()
        assert fields["support_counts"] == {}
        assert fields["dominant_tether"] == "unknown"
        assert fields["thin_support"] is True
        assert "support_evidence" not in fields  # empty -> omitted


class TestBuildEntryNodeEmitsTetherFields:
    def test_emitted_when_present(self):
        model = build_read_model(
            [{"tether": TETHER_SOURCE, "label": "q", "quote_hash": "h"}]
        )
        node = _build_entry_node(_entry(**model.to_entry_fields()))
        assert node["support_counts"] == {"source": 1}
        assert node["dominant_tether"] == "source"
        assert node["thin_support"] is False
        assert node["support_evidence"][0]["tether"] == "source"

    def test_omitted_when_absent_keeps_legacy_shape(self):
        node = _build_entry_node(_entry())
        for key in (
            "support_counts",
            "dominant_tether",
            "thin_support",
            "thin_support_reason",
            "support_evidence",
        ):
            assert key not in node

    def test_thin_support_false_round_trips(self):
        # A bool False must be emitted, not dropped as falsy.
        node = _build_entry_node(
            _entry(thin_support=False, support_counts={"source": 1}, dominant_tether="source")
        )
        assert node["thin_support"] is False
