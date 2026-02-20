"""Unit tests for federation config models in config_schema.py."""

import pytest
from pydantic import ValidationError

from watercooler.config_schema import (
    FederationAccessConfig,
    FederationConfig,
    FederationNamespaceConfig,
    FederationScoringConfig,
    WatercoolerConfig,
)


class TestFederationScoringConfig:
    """Tests for FederationScoringConfig."""

    def test_defaults(self):
        cfg = FederationScoringConfig()
        assert cfg.local_weight == 1.0
        assert cfg.lens_weight == 0.7
        assert cfg.wide_weight == 0.55
        assert cfg.referenced_weight == 0.85
        assert cfg.recency_floor == 0.7
        assert cfg.recency_half_life_days == 60.0

    def test_frozen(self):
        cfg = FederationScoringConfig()
        with pytest.raises(ValidationError):
            cfg.local_weight = 0.5

    def test_recency_floor_bounds(self):
        FederationScoringConfig(recency_floor=0.0)
        FederationScoringConfig(recency_floor=1.0)
        with pytest.raises(ValidationError):
            FederationScoringConfig(recency_floor=-0.1)
        with pytest.raises(ValidationError):
            FederationScoringConfig(recency_floor=1.1)

    def test_recency_half_life_positive(self):
        with pytest.raises(ValidationError):
            FederationScoringConfig(recency_half_life_days=0.0)
        with pytest.raises(ValidationError):
            FederationScoringConfig(recency_half_life_days=-1.0)


class TestFederationNamespaceConfig:
    """Tests for FederationNamespaceConfig."""

    def test_absolute_path(self):
        cfg = FederationNamespaceConfig(code_path="/home/user/project")
        assert cfg.code_path == "/home/user/project"

    def test_relative_path_rejected(self):
        with pytest.raises(ValidationError, match="must be absolute"):
            FederationNamespaceConfig(code_path="relative/path")

    def test_null_bytes_rejected(self):
        with pytest.raises(ValidationError, match="null bytes"):
            FederationNamespaceConfig(code_path="/home/user/\x00evil")

    def test_traversal_resolved(self):
        cfg = FederationNamespaceConfig(code_path="/home/user/../user/project")
        assert ".." not in cfg.code_path

    def test_frozen(self):
        cfg = FederationNamespaceConfig(code_path="/tmp/test")
        with pytest.raises(ValidationError):
            cfg.code_path = "/other"

    def test_default_deny_topics_empty(self):
        cfg = FederationNamespaceConfig(code_path="/tmp/test")
        assert cfg.deny_topics == []

    def test_deny_topics_set(self):
        cfg = FederationNamespaceConfig(
            code_path="/tmp/test",
            deny_topics=["internal-hiring", "salaries"],
        )
        assert cfg.deny_topics == ["internal-hiring", "salaries"]


class TestFederationAccessConfig:
    """Tests for FederationAccessConfig."""

    def test_defaults(self):
        cfg = FederationAccessConfig()
        assert cfg.allowlists == {}

    def test_allowlists(self):
        cfg = FederationAccessConfig(
            allowlists={"cloud": ["site", "docs"]}
        )
        assert cfg.allowlists == {"cloud": ["site", "docs"]}

    def test_frozen(self):
        cfg = FederationAccessConfig()
        with pytest.raises(ValidationError):
            cfg.allowlists = {"new": ["val"]}


class TestFederationConfig:
    """Tests for FederationConfig."""

    def test_defaults(self):
        cfg = FederationConfig()
        assert cfg.enabled is False
        assert cfg.namespaces == {}
        assert cfg.namespace_timeout == 0.4
        assert cfg.max_namespaces == 5
        assert cfg.max_total_timeout == 2.0

    def test_frozen(self):
        cfg = FederationConfig()
        with pytest.raises(ValidationError):
            cfg.enabled = True

    def test_max_namespaces_bounds(self):
        FederationConfig(max_namespaces=1)
        FederationConfig(max_namespaces=20)
        with pytest.raises(ValidationError):
            FederationConfig(max_namespaces=0)
        with pytest.raises(ValidationError):
            FederationConfig(max_namespaces=21)

    def test_namespace_timeout_positive(self):
        with pytest.raises(ValidationError):
            FederationConfig(namespace_timeout=0.0)
        with pytest.raises(ValidationError):
            FederationConfig(namespace_timeout=-1.0)

    def test_basename_collision_rejected(self):
        with pytest.raises(ValidationError, match="basename collision"):
            FederationConfig(
                namespaces={
                    "work-app": FederationNamespaceConfig(code_path="/work/myapp"),
                    "personal-app": FederationNamespaceConfig(code_path="/personal/myapp"),
                }
            )

    def test_no_collision_different_basenames(self):
        cfg = FederationConfig(
            namespaces={
                "cloud": FederationNamespaceConfig(code_path="/home/user/watercooler-cloud"),
                "site": FederationNamespaceConfig(code_path="/home/user/watercooler-site"),
            }
        )
        assert len(cfg.namespaces) == 2

    def test_single_namespace_no_collision(self):
        cfg = FederationConfig(
            namespaces={
                "cloud": FederationNamespaceConfig(code_path="/home/user/watercooler-cloud"),
            }
        )
        assert len(cfg.namespaces) == 1


class TestWatercoolerConfigFederation:
    """Tests for federation field on WatercoolerConfig."""

    def test_default_federation(self):
        cfg = WatercoolerConfig()
        assert cfg.federation.enabled is False
        assert cfg.federation.namespaces == {}

    def test_federation_round_trip(self):
        cfg = WatercoolerConfig(
            federation=FederationConfig(
                enabled=True,
                namespaces={
                    "site": FederationNamespaceConfig(
                        code_path="/home/user/watercooler-site",
                    )
                },
                access=FederationAccessConfig(
                    allowlists={"cloud": ["site"]}
                ),
            )
        )
        assert cfg.federation.enabled is True
        assert "site" in cfg.federation.namespaces
        assert cfg.federation.access.allowlists == {"cloud": ["site"]}
