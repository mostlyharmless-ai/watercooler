"""Tests for the watercooler_acknowledge_finding MCP tool.

PR4 C3 extended the tool with bulk acknowledgment (``finding_ids: list[str]``).
These tests cover the local (non-hosted) dispatch path.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from watercooler_mcp.tools.daemon import _acknowledge_finding_impl


def _run(daemon_name="project_coordinator", *, acked=None, **kwargs):
    """Invoke _acknowledge_finding_impl with the local runtime path mocked.

    ``acked`` is the set of finding IDs the underlying state ack treats as
    successfully acknowledged; all others resolve to not_found.
    """
    acked = set(acked or [])
    with patch(
        "watercooler_mcp.daemons.get_daemon_runtime", return_value=MagicMock()
    ), patch(
        "watercooler_mcp.daemons.ensure_hosted_scope_for_current_context"
    ), patch(
        "watercooler_mcp.daemons.state.acknowledge_finding",
        side_effect=lambda d, f: f in acked,
    ):
        return json.loads(
            _acknowledge_finding_impl(MagicMock(), daemon_name, **kwargs)
        )


def test_requires_a_finding_id():
    data = _run()
    assert data["status"] == "error"
    assert "finding_id" in data["message"]


def test_single_finding_id_acknowledged():
    data = _run(acked={"f1"}, finding_id="f1")
    assert data["status"] == "ok"
    assert data["acknowledged"] == ["f1"]
    assert data["not_found"] == []
    assert data["daemon_name"] == "project_coordinator"


def test_bulk_finding_ids_partial():
    """A mix of acknowledged and not-found IDs yields status=partial."""
    data = _run(acked={"f1", "f3"}, finding_ids=["f1", "f2", "f3"])
    assert data["status"] == "partial"
    assert data["acknowledged"] == ["f1", "f3"]
    assert data["not_found"] == ["f2"]


def test_bulk_all_not_found():
    data = _run(acked=set(), finding_ids=["f1", "f2"])
    assert data["status"] == "not_found"
    assert data["acknowledged"] == []
    assert data["not_found"] == ["f1", "f2"]


def test_finding_id_and_finding_ids_combine_and_dedup():
    """finding_id is merged with finding_ids; duplicates collapse, order kept."""
    data = _run(
        acked={"f1", "f2"}, finding_id="f1", finding_ids=["f1", "f2"]
    )
    assert data["status"] == "ok"
    assert data["acknowledged"] == ["f1", "f2"]


def test_blank_ids_are_ignored():
    data = _run(finding_id="   ", finding_ids=["", "  "])
    assert data["status"] == "error"


def test_daemon_findings_action_acknowledge_dispatches():
    """PR5 D1 — daemon_findings(action="acknowledge") routes to the folded-in
    acknowledge implementation, using `daemon` as the owning daemon name."""
    from watercooler_mcp.tools.daemon import _daemon_findings_impl

    with patch(
        "watercooler_mcp.daemons.get_daemon_runtime", return_value=MagicMock()
    ), patch(
        "watercooler_mcp.daemons.ensure_hosted_scope_for_current_context"
    ), patch(
        "watercooler_mcp.daemons.state.acknowledge_finding",
        side_effect=lambda d, f: f == "f1",
    ):
        data = json.loads(
            _daemon_findings_impl(
                MagicMock(),
                daemon="project_coordinator",
                action="acknowledge",
                finding_ids=["f1", "f2"],
            )
        )
    assert data["acknowledged"] == ["f1"]
    assert data["not_found"] == ["f2"]
    assert data["daemon_name"] == "project_coordinator"
