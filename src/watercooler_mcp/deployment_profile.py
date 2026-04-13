"""Deployment profile resolver for hosted MCP services.

Resolves the effective deployment profile (what a hosted deployment can
actually execute) by inspecting build-time markers, requested profile,
and runtime availability of backends.  Strict downgrade rules ensure the
effective profile never exceeds what is structurally available.

All checks are structural (importability + env-var presence) — no live
network probes — so this module is safe to call at startup.

Environment variables consumed:
- WATERCOOLER_BUILD_PROFILE: Build-time tier ("core", "t2", "t2t3").
- WATERCOOLER_HOSTED_PROFILE: Requested runtime tier ("core", "t2", "t2t3").
- FALKORDB_HOST: FalkorDB connection target (t2 requirement).
- LLM_API_BASE: LLM inference endpoint (t2 requirement).
- EMBEDDING_API_BASE: Embedding inference endpoint (t2 requirement).
- WATERCOOLER_TOKEN_API_URL: Token service URL (t2 requirement).
- WATERCOOLER_TOKEN_API_KEY: Token service key (t2 requirement).
- WATERCOOLER_CAPABILITY_API_URL: Capability grant API URL.
- WATERCOOLER_CAPABILITY_API_KEY: Capability grant API key.
- WATERCOOLER_FINDINGS_API_URL: Daemon findings API URL.
- WATERCOOLER_FINDINGS_API_KEY: Daemon findings API key.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

BuildProfile = Literal["core", "t2", "t2t3"]
HostedProfile = Literal["core", "t2", "t2t3"]

_VALID_PROFILES = frozenset({"core", "t2", "t2t3"})

# Ordered from least to most capable — used for ceiling checks.
_PROFILE_RANK: dict[str, int] = {"core": 0, "t2": 1, "t2t3": 2}


# ---------------------------------------------------------------------------
# Availability dataclass
# ---------------------------------------------------------------------------


@dataclass
class DeploymentAvailability:
    """Result of resolving which profile a deployment can actually use.

    Attributes:
      build_profile: Profile baked into the build artifact.
      requested_profile: Profile requested via env var.
      effective_profile: Profile that will actually be used after
          downgrade rules are applied.
      graphiti_available: Whether the Graphiti backend is importable.
      leanrag_available: Whether the LeanRAG backend is importable.
      persistent_state_available: Whether the persistent state dir is
          writable (``~/.watercooler``).
      token_api_configured: Whether the token service env vars are set.
      capability_api_configured: Whether the capability grant API env
          vars are set.
      findings_api_configured: Whether the daemon findings API env vars
          are set.
      degraded_reasons: Human-readable reasons for any downgrade from
          the requested profile.
    """

    build_profile: str
    requested_profile: str
    effective_profile: str
    graphiti_available: bool
    leanrag_available: bool
    persistent_state_available: bool
    token_api_configured: bool
    capability_api_configured: bool
    findings_api_configured: bool
    degraded_reasons: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Profile readers
# ---------------------------------------------------------------------------


def get_build_profile() -> BuildProfile:
    """Read the build-time profile from the environment.

    Returns:
      The validated build profile, defaulting to ``"core"`` when the
      env var is absent or invalid.
    """
    raw = os.getenv("WATERCOOLER_BUILD_PROFILE", "core").strip().lower()
    if raw not in _VALID_PROFILES:
        logger.warning(
            "Invalid WATERCOOLER_BUILD_PROFILE=%r, falling back to 'core'.",
            raw,
        )
        return "core"
    return raw  # type: ignore[return-value]


def get_requested_hosted_profile() -> HostedProfile:
    """Read the requested hosted profile from the environment.

    Returns:
      The validated hosted profile, defaulting to ``"core"`` when the
      env var is absent or invalid.
    """
    raw = os.getenv("WATERCOOLER_HOSTED_PROFILE", "core").strip().lower()
    if raw not in _VALID_PROFILES:
        logger.warning(
            "Invalid WATERCOOLER_HOSTED_PROFILE=%r, falling back to 'core'.",
            raw,
        )
        return "core"
    return raw  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Structural probes (no network I/O)
# ---------------------------------------------------------------------------


def _check_graphiti_importable() -> bool:
    """Return True if the Graphiti backend module can be imported."""
    try:
        import watercooler_memory.backends.graphiti  # noqa: F401

        return True
    except Exception:
        return False


def _check_leanrag_importable() -> bool:
    """Return True if the LeanRAG package can be imported."""
    try:
        import leanrag  # noqa: F401

        return True
    except Exception:
        return False


def _check_persistent_state_writable() -> bool:
    """Return True if ``~/.watercooler`` exists and is writable."""
    state_dir = Path.home() / ".watercooler"
    try:
        return state_dir.is_dir() and os.access(state_dir, os.W_OK)
    except Exception:
        return False


def _env_pair_set(url_var: str, key_var: str) -> bool:
    """Return True if both *url_var* and *key_var* are non-empty."""
    return bool(os.getenv(url_var, "").strip()) and bool(os.getenv(key_var, "").strip())


# ---------------------------------------------------------------------------
# Main resolver
# ---------------------------------------------------------------------------


def resolve_deployment_availability(
    code_path: str = "",
) -> DeploymentAvailability:
    """Resolve the effective deployment profile.

    Inspects the build profile, requested profile, and runtime
    availability of each backend.  The effective profile is the
    highest tier that passes all structural checks without exceeding
    the build profile.

    Args:
      code_path: Optional repository root (reserved for future
          per-repo overrides).

    Returns:
      A ``DeploymentAvailability`` describing the resolved state.
    """
    build = get_build_profile()
    requested = get_requested_hosted_profile()
    reasons: list[str] = []

    # -- structural probes --------------------------------------------------
    graphiti_ok = _check_graphiti_importable()
    falkordb_ok = bool(os.getenv("FALKORDB_HOST", "").strip())
    llm_ok = bool(os.getenv("LLM_API_BASE", "").strip())
    embedding_ok = bool(os.getenv("EMBEDDING_API_BASE", "").strip())
    token_api_ok = _env_pair_set(
        "WATERCOOLER_TOKEN_API_URL", "WATERCOOLER_TOKEN_API_KEY"
    )
    persistent_ok = _check_persistent_state_writable()
    leanrag_ok = _check_leanrag_importable()
    capability_api_ok = _env_pair_set(
        "WATERCOOLER_CAPABILITY_API_URL", "WATERCOOLER_CAPABILITY_API_KEY"
    )
    findings_api_ok = _env_pair_set(
        "WATERCOOLER_FINDINGS_API_URL", "WATERCOOLER_FINDINGS_API_KEY"
    )

    # -- t2 gate: every check must pass ------------------------------------
    t2_checks: list[tuple[bool, str]] = [
        (graphiti_ok, "Graphiti backend not importable"),
        (falkordb_ok, "FALKORDB_HOST not set"),
        (llm_ok, "LLM_API_BASE not set"),
        (embedding_ok, "EMBEDDING_API_BASE not set"),
        (token_api_ok, "Token API not configured (WATERCOOLER_TOKEN_API_URL / KEY)"),
        (persistent_ok, "~/.watercooler not writable"),
    ]
    t2_pass = all(ok for ok, _ in t2_checks)

    # -- resolve effective profile ------------------------------------------
    if requested == "core":
        effective = "core"

    elif requested == "t2":
        if not t2_pass:
            effective = "core"
            for ok, reason in t2_checks:
                if not ok:
                    reasons.append(reason)
        elif _PROFILE_RANK[build] < _PROFILE_RANK["t2"]:
            effective = "core"
            reasons.append(f"Build profile '{build}' does not include t2 capabilities")
        else:
            effective = "t2"

    elif requested == "t2t3":
        if not t2_pass:
            # Can't even reach t2 — fall all the way to core.
            effective = "core"
            for ok, reason in t2_checks:
                if not ok:
                    reasons.append(reason)
        elif _PROFILE_RANK[build] < _PROFILE_RANK["t2"]:
            effective = "core"
            reasons.append(f"Build profile '{build}' does not include t2 capabilities")
        elif not leanrag_ok:
            # t2 passes but LeanRAG missing — settle for t2.
            effective = "t2"
            reasons.append("LeanRAG not importable; downgraded from t2t3 to t2")
            # Still cap at build profile.
            if _PROFILE_RANK[build] < _PROFILE_RANK["t2"]:
                effective = "core"
                reasons.append(
                    f"Build profile '{build}' does not include t2 capabilities"
                )
        elif _PROFILE_RANK[build] < _PROFILE_RANK["t2t3"]:
            effective = "t2"
            reasons.append(
                f"Build profile '{build}' does not include t2t3 capabilities; "
                f"capped at t2"
            )
        else:
            effective = "t2t3"

    else:
        # Unknown requested profile — treat as core.
        effective = "core"
        reasons.append(f"Unknown requested profile '{requested}'; defaulting to core")

    # Final ceiling: effective must never exceed build.
    if _PROFILE_RANK.get(effective, 0) > _PROFILE_RANK.get(build, 0):
        reasons.append(f"Effective '{effective}' exceeds build '{build}'; capped")
        effective = build

    if reasons:
        logger.info(
            "Deployment downgraded from '%s' to '%s': %s",
            requested,
            effective,
            "; ".join(reasons),
        )

    return DeploymentAvailability(
        build_profile=build,
        requested_profile=requested,
        effective_profile=effective,
        graphiti_available=graphiti_ok,
        leanrag_available=leanrag_ok,
        persistent_state_available=persistent_ok,
        token_api_configured=token_api_ok,
        capability_api_configured=capability_api_ok,
        findings_api_configured=findings_api_ok,
        degraded_reasons=reasons,
    )


# ---------------------------------------------------------------------------
# Operation gate
# ---------------------------------------------------------------------------


def operation_supported_by_profile(
    operation_descriptor: Any,
    availability: DeploymentAvailability,
) -> bool:
    """Check whether an operation can execute under the effective profile.

    The *operation_descriptor* is any object (typically a dataclass or
    namespace) exposing boolean attributes that declare its requirements:

    - ``requires_graphiti`` — needs the Graphiti/FalkorDB stack (t2+).
    - ``requires_leanrag`` — needs the LeanRAG pipeline (t2t3).
    - ``hosted_safe`` — safe to run in a hosted (multi-tenant) context.

    Missing attributes are treated as ``False`` (i.e. no requirement).

    Args:
      operation_descriptor: Object describing the operation's
          requirements.
      availability: The resolved deployment availability.

    Returns:
      ``True`` if the operation can execute; ``False`` otherwise.
    """
    eff = availability.effective_profile

    if getattr(operation_descriptor, "requires_graphiti", False) and eff == "core":
        return False

    if getattr(operation_descriptor, "requires_leanrag", False) and eff in (
        "core",
        "t2",
    ):
        return False

    # Hosted-safety: if this is a hosted deployment and the operation is
    # not marked safe, reject it.  A deployment is "hosted" when the token
    # API is configured (same heuristic used by auth.is_hosted_mode).
    if availability.token_api_configured and not getattr(
        operation_descriptor, "hosted_safe", False
    ):
        return False

    return True
