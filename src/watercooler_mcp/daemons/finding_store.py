"""Finding store abstraction for local persistence.

Provides a pluggable interface for persisting daemon findings:

- ``LocalFindingStore``: Wraps JSONL-based persistence in ``state.py``.

Move 3 PR β (security consolidation plan v5.1) removed the
``HostedFindingStore`` remote backend. PR α had shipped the
observation-window WARNING + telemetry counter
(``security.consolidation.m3.hosted_finding_store_init``) for one
minor-version cycle; production logs showed zero instantiations,
and a code-grep confirmed only test code referenced the class. The
class is gone; only the local JSONL path remains.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from .state import Finding

logger = logging.getLogger(__name__)


class FindingStore(ABC):
    """Abstract finding persistence interface."""

    @abstractmethod
    def append_or_refresh(self, finding: Finding) -> None:
        """Persist or update a finding (upsert by finding_id)."""

    @abstractmethod
    def query(
        self,
        daemon_name: str = "",
        severity: str = "",
        category: str = "",
        topic: str = "",
        limit: int = 100,
        unacknowledged_only: bool = False,
    ) -> list[Finding]:
        """Query findings with optional filters."""

    @abstractmethod
    def acknowledge(self, daemon_name: str, finding_id: str) -> bool:
        """Mark a finding as acknowledged. Returns True on success."""


class LocalFindingStore(FindingStore):
    """JSONL-based local finding store.

    Wraps the existing ``append_findings`` / ``load_findings`` helpers
    from ``state.py`` with namespace awareness.
    """

    def __init__(self, namespace: str = "") -> None:
        self._namespace = namespace

    def append_or_refresh(self, finding: Finding) -> None:
        from .state import append_findings

        append_findings(finding.daemon_name, [finding], namespace=self._namespace)

    def query(
        self,
        daemon_name: str = "",
        severity: str = "",
        category: str = "",
        topic: str = "",
        limit: int = 100,
        unacknowledged_only: bool = False,
    ) -> list[Finding]:
        from .state import load_findings

        if not daemon_name:
            return []
        return load_findings(
            daemon_name,
            limit=limit,
            severity=severity or None,
            category=category or None,
            topic=topic or None,
            unacknowledged_only=unacknowledged_only,
            namespace=self._namespace,
        )

    def acknowledge(self, daemon_name: str, finding_id: str) -> bool:
        from .state import acknowledge_finding

        return acknowledge_finding(daemon_name, finding_id, namespace=self._namespace)
