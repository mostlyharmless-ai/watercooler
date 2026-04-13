"""Finding store abstraction for local and hosted persistence.

Provides a pluggable interface for persisting daemon findings:

- ``LocalFindingStore``: Wraps existing JSONL-based persistence.
- ``HostedFindingStore``: Uses stdlib ``urllib`` to call the
  watercooler-site API (``/api/mcp/daemon-findings/*``).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

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
    def acknowledge(self, finding_id: str) -> bool:
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

    def acknowledge(self, finding_id: str) -> bool:
        from .state import acknowledge_finding
        return acknowledge_finding(finding_id)


class HostedFindingStore(FindingStore):
    """Remote finding store using the watercooler-site API.

    Endpoints:
    - ``POST /api/mcp/daemon-findings/upsert``
    - ``GET  /api/mcp/daemon-findings``
    - ``PATCH /api/mcp/daemon-findings/<findingId>``

    Uses ``finding_id`` as the dedup key.  Re-observed findings update
    ``last_seen_at`` and ``occurrence_count`` rather than creating
    duplicates.
    """

    def __init__(self, api_url: str, api_key: str = "", vercel_bypass_secret: str = "") -> None:
        self._api_url = api_url.rstrip("/")
        self._api_key = api_key
        self._vercel_bypass_secret = vercel_bypass_secret

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._api_key:
            h["x-api-key"] = self._api_key
        if self._vercel_bypass_secret:
            h["x-vercel-protection-bypass"] = self._vercel_bypass_secret
        return h

    def append_or_refresh(self, finding: Finding) -> None:
        url = f"{self._api_url}/api/mcp/daemon-findings/upsert"
        payload = json.dumps(finding.to_dict()).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST")
        for k, v in self._headers().items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=10):
                pass
        except Exception as exc:
            logger.warning("HostedFindingStore upsert failed: %s", exc)

    def query(
        self,
        daemon_name: str = "",
        severity: str = "",
        category: str = "",
        topic: str = "",
        limit: int = 100,
        unacknowledged_only: bool = False,
    ) -> list[Finding]:
        params: list[str] = []
        if daemon_name:
            params.append(f"daemon_name={urllib.parse.quote(daemon_name)}")
        if severity:
            params.append(f"severity={urllib.parse.quote(severity)}")
        if category:
            params.append(f"category={urllib.parse.quote(category)}")
        if topic:
            params.append(f"topic={urllib.parse.quote(topic)}")
        if unacknowledged_only:
            params.append("acknowledged=false")
        params.append(f"limit={limit}")

        url = f"{self._api_url}/api/mcp/daemon-findings?{'&'.join(params)}"
        req = urllib.request.Request(url, method="GET")
        for k, v in self._headers().items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return [Finding.from_dict(d) for d in data.get("findings", [])]
        except Exception as exc:
            logger.warning("HostedFindingStore query failed: %s", exc)
            return []

    def acknowledge(self, finding_id: str) -> bool:
        url = f"{self._api_url}/api/mcp/daemon-findings/{urllib.parse.quote(finding_id)}"
        payload = json.dumps({"acknowledged": True}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="PATCH")
        for k, v in self._headers().items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=10):
                return True
        except Exception as exc:
            logger.warning("HostedFindingStore acknowledge failed: %s", exc)
            return False
