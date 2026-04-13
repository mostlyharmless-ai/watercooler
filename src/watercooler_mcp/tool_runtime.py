"""Tool runtime context for capability-aware server surfaces.

Each server surface (local_full, local_hybrid, hosted_full, hosted_premium)
receives a frozen ToolRuntime that carries:

- The surface identity
- The CapabilityProfile (route table)
- An optional PremiumToolClient (for hybrid/hosted surfaces)
- An optional CapabilityAuthorizer (for hosted surfaces)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Optional

from .capabilities import CapabilityProfile

if TYPE_CHECKING:
    from .premium_client import PremiumToolClient
    from .capability_auth import CapabilityAuthorizer
    from .deployment_profile import DeploymentAvailability

SurfaceName = Literal["local_full", "local_hybrid", "hosted_full", "hosted_premium"]


@dataclass(frozen=True)
class ToolRuntime:
    """Immutable runtime context shared across all tools on a surface."""

    surface: SurfaceName
    capability_profile: CapabilityProfile = field(default_factory=CapabilityProfile)
    premium_client: Optional[PremiumToolClient] = None
    authorizer: Optional[CapabilityAuthorizer] = None
    deployment_availability: Optional[DeploymentAvailability] = None

    @property
    def effective_hosted_profile(self) -> str:
        """Return the effective hosted profile, or 'core' if no availability."""
        if self.deployment_availability is not None:
            return self.deployment_availability.effective_profile
        return "core"
