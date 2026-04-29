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

    def __init__(
        self, api_url: str, api_key: str = "", vercel_bypass_secret: str = ""
    ) -> None:
        # Move 3 PR α (security consolidation plan v5.1): observation
        # window before deletion. The audit confirmed zero production
        # callers of HostedFindingStore — only test code instantiates
        # it. This WARNING + telemetry counter runs for one
        # minor-version cycle so PR β (deletion in Sprint 3) can
        # commit-message-document the negative-instantiation result
        # before removing the class. Bumping the counter rather than
        # raising keeps the test surface intact for the observation
        # period.
        logger.warning(
            "HostedFindingStore instantiated — this class is scheduled "
            "for removal in the security-consolidation Move 3 PR β "
            "(no production callers expected). If you see this in "
            "production logs, surface it to the security thread "
            "before the next minor-version cut."
        )
        try:
            from ..auth.scope import strip_url_credentials
            from ..observability import log_action

            # ``api_url`` is caller-supplied and may embed credentials
            # (e.g., ``https://user:token@host``). Strip them before
            # the value reaches telemetry — the same primitive the
            # canonical-stdio-namespace pipeline uses for git remotes.
            log_action(
                "security.consolidation.m3.hosted_finding_store_init",
                outcome="observed",
                api_url=strip_url_credentials(api_url),
            )
        except Exception:  # noqa: BLE001
            # Telemetry must never break construction; observability
            # may be partially initialised in some test paths.
            pass

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

    def acknowledge(self, daemon_name: str, finding_id: str) -> bool:
        url = (
            f"{self._api_url}/api/mcp/daemon-findings/{urllib.parse.quote(finding_id)}"
        )
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
