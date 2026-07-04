"""D5: superseded_by_ratified is derived from the xref_supersedes annotation, not a flag.

``_supersession_is_ratified`` is authored iff an ``xref_supersedes`` annotation on the
superseded entry records the successor — the durable, append-only RFC-P3 signal that
replaced the removed mutable T2 ``superseded_ratified`` flag. It must work on BOTH surfaces:
local (filesystem baseline graph) and hosted (annotations on GitHub) — else a hosted
ratification never flips the production badge. Degrades to False (afforded) on any read
failure — never a false authored (§6.5).
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from watercooler.baseline_graph.annotations import AnnotationState
import watercooler_mcp.tools.decisions as d

_CTX = SimpleNamespace(threads_dir=Path("/tmp/wc-test-threads"))


def _local_ctx():
    # is_hosted_context → False for the local branch.
    return patch.object(d, "is_hosted_context", return_value=False)


def _hosted_ctx():
    return patch.object(d, "is_hosted_context", return_value=True)


def _require_ok():
    return patch.object(d.validation, "_require_context", return_value=(None, _CTX))


# --- local (filesystem) branch ---

def test_local_authored_when_annotation_records_successor():
    with _require_ok(), _local_ctx(), patch(
        "watercooler.baseline_graph.annotations.get_annotation_state",
        return_value=AnnotationState(xref_supersedes=["01B"]),
    ):
        assert d._supersession_is_ratified("topic-a", "01A", "01B", "/repo") is True


def test_local_afforded_when_annotation_absent():
    with _require_ok(), _local_ctx(), patch(
        "watercooler.baseline_graph.annotations.get_annotation_state",
        return_value=AnnotationState(xref_supersedes=[]),
    ):
        assert d._supersession_is_ratified("topic-a", "01A", "01B", "/repo") is False


# --- hosted (GitHub) branch — the review's blocking case ---

def test_hosted_authored_reads_back_from_github():
    with _require_ok(), _hosted_ctx(), patch(
        "watercooler_mcp.hosted_ops.get_annotations_hosted",
        return_value=(None, {"annotation_state": {"xref_supersedes": ["01B"]}}),
    ):
        assert d._supersession_is_ratified("topic-a", "01A", "01B", "/repo") is True


def test_hosted_afforded_when_successor_not_recorded():
    with _require_ok(), _hosted_ctx(), patch(
        "watercooler_mcp.hosted_ops.get_annotations_hosted",
        return_value=(None, {"annotation_state": {"xref_supersedes": ["01OTHER"]}}),
    ):
        assert d._supersession_is_ratified("topic-a", "01A", "01B", "/repo") is False


def test_hosted_afforded_on_github_read_error():
    with _require_ok(), _hosted_ctx(), patch(
        "watercooler_mcp.hosted_ops.get_annotations_hosted",
        return_value=("rate limited", None),
    ):
        assert d._supersession_is_ratified("topic-a", "01A", "01B", "/repo") is False


# --- degradation (both branches) ---

def test_degrades_to_afforded_on_context_error():
    with patch.object(d.validation, "_require_context", return_value=("boom", None)):
        assert d._supersession_is_ratified("topic-a", "01A", "01B", "/repo") is False


def test_missing_topic_or_successor_is_afforded():
    assert d._supersession_is_ratified("", "01A", "01B", "/repo") is False
    assert d._supersession_is_ratified("topic-a", "01A", "", "/repo") is False
