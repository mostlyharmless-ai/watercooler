"""Unit tests for federation access control module."""

from watercooler.config_schema import FederationAccessConfig, FederationNamespaceConfig
from watercooler_mcp.federation.access import filter_allowed_namespaces, is_topic_denied


class TestFilterAllowedNamespaces:
    """Tests for filter_allowed_namespaces."""

    def test_primary_always_allowed(self):
        access = FederationAccessConfig(allowlists={})
        allowed, denied = filter_allowed_namespaces("cloud", ["cloud"], access)
        assert allowed == ["cloud"]
        assert denied == {}

    def test_secondary_in_allowlist(self):
        access = FederationAccessConfig(allowlists={"cloud": ["site"]})
        allowed, denied = filter_allowed_namespaces(
            "cloud", ["cloud", "site"], access
        )
        assert allowed == ["cloud", "site"]
        assert denied == {}

    def test_secondary_not_in_allowlist(self):
        access = FederationAccessConfig(allowlists={"cloud": ["site"]})
        allowed, denied = filter_allowed_namespaces(
            "cloud", ["cloud", "site", "docs"], access
        )
        assert allowed == ["cloud", "site"]
        assert denied == {"docs": "access_denied"}

    def test_no_allowlist_for_primary_denies_all_secondaries(self):
        access = FederationAccessConfig(allowlists={})
        allowed, denied = filter_allowed_namespaces(
            "cloud", ["cloud", "site", "docs"], access
        )
        assert allowed == ["cloud"]
        assert denied == {"site": "access_denied", "docs": "access_denied"}

    def test_empty_allowlist_for_primary_denies_all(self):
        access = FederationAccessConfig(allowlists={"cloud": []})
        allowed, denied = filter_allowed_namespaces(
            "cloud", ["cloud", "site"], access
        )
        assert allowed == ["cloud"]
        assert denied == {"site": "access_denied"}

    def test_all_secondaries_denied(self):
        access = FederationAccessConfig(allowlists={})
        allowed, denied = filter_allowed_namespaces(
            "cloud", ["cloud", "site", "docs"], access
        )
        assert allowed == ["cloud"]
        assert len(denied) == 2

    def test_primary_only_no_secondaries(self):
        access = FederationAccessConfig(allowlists={"cloud": ["site"]})
        allowed, denied = filter_allowed_namespaces("cloud", ["cloud"], access)
        assert allowed == ["cloud"]
        assert denied == {}

    def test_preserves_order(self):
        access = FederationAccessConfig(allowlists={"cloud": ["docs", "site"]})
        allowed, denied = filter_allowed_namespaces(
            "cloud", ["cloud", "site", "docs"], access
        )
        # Primary first, then secondaries in requested order
        assert allowed == ["cloud", "site", "docs"]


class TestIsTopicDenied:
    """Tests for is_topic_denied."""

    def test_not_denied_when_empty(self):
        ns = FederationNamespaceConfig(code_path="/tmp/test")
        assert is_topic_denied("auth-protocol", "site", ns) is False

    def test_denied_exact_match(self):
        ns = FederationNamespaceConfig(
            code_path="/tmp/test", deny_topics=["internal-hiring"]
        )
        assert is_topic_denied("internal-hiring", "site", ns) is True

    def test_denied_case_insensitive(self):
        ns = FederationNamespaceConfig(
            code_path="/tmp/test", deny_topics=["internal-hiring"]
        )
        assert is_topic_denied("Internal-Hiring", "site", ns) is True
        assert is_topic_denied("INTERNAL-HIRING", "site", ns) is True

    def test_not_denied_partial_match(self):
        ns = FederationNamespaceConfig(
            code_path="/tmp/test", deny_topics=["internal-hiring"]
        )
        assert is_topic_denied("internal", "site", ns) is False

    def test_multiple_deny_topics(self):
        ns = FederationNamespaceConfig(
            code_path="/tmp/test", deny_topics=["salaries", "internal-hiring"]
        )
        assert is_topic_denied("salaries", "site", ns) is True
        assert is_topic_denied("internal-hiring", "site", ns) is True
        assert is_topic_denied("auth-protocol", "site", ns) is False
