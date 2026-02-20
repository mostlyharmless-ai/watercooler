"""Access control for federated search.

Allowlist + deny_topics enforcement per namespace.
"""

from __future__ import annotations

from watercooler.config_schema import FederationAccessConfig, FederationNamespaceConfig

__all__ = [
    "filter_allowed_namespaces",
    "is_topic_denied",
]


def filter_allowed_namespaces(
    primary_namespace: str,
    requested_namespaces: list[str],
    access_config: FederationAccessConfig,
) -> tuple[list[str], dict[str, str]]:
    """Filter requested namespaces against per-primary allowlist.

    The primary namespace is always allowed. Secondary namespaces are
    checked against the allowlist for the primary namespace.

    Default: closed — if the primary has no entry in allowlists, no
    secondary access is granted.

    Args:
        primary_namespace: The primary namespace ID.
        requested_namespaces: All namespace IDs to search.
        access_config: Access control configuration.

    Returns:
        (allowed_namespaces, denied_map)
        denied_map: {namespace: "access_denied"} for blocked namespaces.
    """
    allowed_secondaries = set(
        access_config.allowlists.get(primary_namespace, [])
    )

    allowed: list[str] = []
    denied: dict[str, str] = {}

    for ns in requested_namespaces:
        if ns == primary_namespace:
            allowed.append(ns)
        elif ns in allowed_secondaries:
            allowed.append(ns)
        else:
            denied[ns] = "access_denied"

    return allowed, denied


def is_topic_denied(
    topic: str,
    namespace: str,
    namespace_config: FederationNamespaceConfig,
) -> bool:
    """Check if a topic is denied for a namespace.

    Case-insensitive comparison: normalizes both topic and deny_topics
    to lowercase before matching.

    Args:
        topic: The thread topic to check.
        namespace: The namespace ID (unused, for future logging).
        namespace_config: Namespace configuration with deny_topics.

    Returns:
        True if the topic should be excluded.
    """
    topic_lower = topic.lower()
    return any(dt.lower() == topic_lower for dt in namespace_config.deny_topics)
