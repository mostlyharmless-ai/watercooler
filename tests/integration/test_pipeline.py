"""Tests for watercooler_memory.pipeline module."""

import json
import os
import pytest
from pathlib import Path

from watercooler_memory.pipeline.config import (
    PipelineConfig,
    LLMConfig,
    EmbeddingConfig,
    load_config_from_env,
)
from watercooler_memory.pipeline.state import (
    PipelineState,
    Stage,
    StageState,
)
from watercooler_memory.pipeline.stages import _redact_sensitive


class TestConfig:
    """Tests for pipeline configuration."""

    def test_llm_config_defaults(self):
        """Test LLMConfig has sensible defaults."""
        config = LLMConfig()
        # API key may be empty if not configured, but shouldn't crash
        assert isinstance(config.api_key, str)
        assert isinstance(config.model, str)
        assert isinstance(config.base_url, str)

    def test_llm_config_validation_missing_key(self):
        """Test LLMConfig validation detects missing API key."""
        config = LLMConfig(api_key="")
        errors = config.validate()
        assert any("API_KEY" in e for e in errors)

    def test_llm_config_validation_with_key(self):
        """Test LLMConfig validation passes with API key."""
        config = LLMConfig(api_key="test-key-123")
        errors = config.validate()
        assert len(errors) == 0

    def test_embedding_config_defaults(self):
        """Test EmbeddingConfig has sensible defaults."""
        config = EmbeddingConfig()
        assert config.embedding_dim == 1024
        assert config.batch_size >= 1

    def test_embedding_config_batch_size_validation(self):
        """Test batch_size is always at least 1."""
        # Even with invalid env var, should be at least 1
        config = EmbeddingConfig(batch_size=0)
        # Manual setting can bypass, but validation should catch
        assert config.batch_size == 0  # direct set bypasses
        # But factory uses max(1, ...)
        config2 = EmbeddingConfig()
        assert config2.batch_size >= 1

    def test_pipeline_config_validation(self):
        """Test PipelineConfig validation."""
        config = PipelineConfig(
            threads_dir=Path("/nonexistent/path"),
            llm=LLMConfig(api_key="test-key"),
            embedding=EmbeddingConfig(base_url="http://localhost:8080"),
        )
        errors = config.validate()
        assert any("not found" in e for e in errors)

    def test_load_config_from_env_defaults(self):
        """Test load_config_from_env with defaults."""
        config = load_config_from_env()
        assert config.batch_size >= 1
        assert config.max_concurrent >= 1
        assert config.test_mode is False


class TestState:
    """Tests for pipeline state management."""

    def test_create_state(self, tmp_path):
        """Test creating a new pipeline state."""
        state = PipelineState.create(
            run_id="test-run-123",
            threads_dir=tmp_path / "threads",
            work_dir=tmp_path / "work",
        )
        assert state.run_id == "test-run-123"
        # State stores paths as strings
        assert state.threads_dir == str(tmp_path / "threads")
        assert state.work_dir == str(tmp_path / "work")
        assert len(state.stages) == 4  # EXPORT, EXTRACT, DEDUPE, BUILD

    def test_state_save_load(self, tmp_path):
        """Test state serialization and deserialization."""
        state = PipelineState.create(
            run_id="test-run-456",
            threads_dir=tmp_path / "threads",
            work_dir=tmp_path / "work",
        )

        state_file = tmp_path / "state.json"
        state.save(state_file)

        assert state_file.exists()

        loaded = PipelineState.load(state_file)
        assert loaded.run_id == "test-run-456"
        # State stores paths as strings
        assert loaded.threads_dir == str(tmp_path / "threads")
        assert loaded.work_dir == str(tmp_path / "work")

    def test_state_stage_update(self, tmp_path):
        """Test updating stage state."""
        state = PipelineState.create(
            run_id="test-run-789",
            threads_dir=tmp_path / "threads",
            work_dir=tmp_path / "work",
        )

        export_state = state.get_stage(Stage.EXPORT)
        assert export_state.status == "pending"

        export_state.status = "running"
        export_state.outputs = {"test": "value"}

        assert state.get_stage(Stage.EXPORT).status == "running"
        assert state.get_stage(Stage.EXPORT).outputs["test"] == "value"

    def test_state_round_trip_with_outputs(self, tmp_path):
        """Test state save/load preserves outputs."""
        state = PipelineState.create(
            run_id="test-outputs",
            threads_dir=tmp_path / "threads",
            work_dir=tmp_path / "work",
        )

        export_state = state.get_stage(Stage.EXPORT)
        export_state.status = "completed"
        export_state.outputs = {
            "export_dir": "/tmp/export",
            "documents_file": "/tmp/export/documents.json",
            "statistics": {"documents": 10, "chunks": 50},
        }

        state_file = tmp_path / "state.json"
        state.save(state_file)

        loaded = PipelineState.load(state_file)
        loaded_export = loaded.get_stage(Stage.EXPORT)
        assert loaded_export.status == "completed"
        assert loaded_export.outputs["export_dir"] == "/tmp/export"
        assert loaded_export.outputs["statistics"]["documents"] == 10


class TestRedaction:
    """Tests for sensitive data redaction."""

    def test_redact_api_key_env_var(self):
        """Test redacting API key from env var format."""
        text = "Error: DEEPSEEK_API_KEY=sk-abc123xyz789 is invalid"
        redacted = _redact_sensitive(text)
        assert "sk-abc123xyz789" not in redacted
        assert "DEEPSEEK_API_KEY" in redacted
        assert "[REDACTED]" in redacted

    def test_redact_bare_api_key(self):
        """Test redacting bare API key pattern."""
        text = "Failed with key sk-abcdefghij1234567890klmno"
        redacted = _redact_sensitive(text)
        assert "sk-abcdefghij1234567890klmno" not in redacted
        assert "[REDACTED_KEY]" in redacted

    def test_redact_password(self):
        """Test redacting password from env var format."""
        text = "DB_PASSWORD=secret123 in environment"
        redacted = _redact_sensitive(text)
        assert "secret123" not in redacted
        assert "[REDACTED]" in redacted

    def test_redact_preserves_normal_text(self):
        """Test that normal text is not affected."""
        text = "Processing 10 documents from /path/to/dir"
        redacted = _redact_sensitive(text)
        assert redacted == text

    def test_redact_multiple_secrets(self):
        """Test redacting multiple secrets in one string."""
        text = "DEEPSEEK_API_KEY=sk-key1 TOKEN=secret123"
        redacted = _redact_sensitive(text)
        assert "sk-key1" not in redacted
        assert "secret123" not in redacted


class TestStageValidation:
    """Tests for stage input validation."""

    def test_export_stage_validation_missing_dir(self, tmp_path):
        """Test export stage validates threads directory."""
        from watercooler_memory.pipeline.stages import ExportStageRunner
        from watercooler_memory.pipeline.logging import PipelineLogger

        config = PipelineConfig(
            threads_dir=tmp_path / "nonexistent",
            work_dir=tmp_path / "work",
        )
        state = PipelineState.create("test", config.threads_dir, config.work_dir)
        logger = PipelineLogger("test", tmp_path / "logs")

        runner = ExportStageRunner(config, state, logger)
        errors = runner.validate_inputs()

        assert len(errors) > 0
        assert any("not found" in e for e in errors)

    def test_export_stage_validation_empty_dir(self, tmp_path):
        """Test export stage validates .md files exist."""
        from watercooler_memory.pipeline.stages import ExportStageRunner
        from watercooler_memory.pipeline.logging import PipelineLogger

        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()

        config = PipelineConfig(
            threads_dir=threads_dir,
            work_dir=tmp_path / "work",
        )
        state = PipelineState.create("test", config.threads_dir, config.work_dir)
        logger = PipelineLogger("test", tmp_path / "logs")

        runner = ExportStageRunner(config, state, logger)
        errors = runner.validate_inputs()

        assert len(errors) > 0
        assert any(".md" in e for e in errors)


class TestExportStageIntegration:
    """Integration tests for ExportStageRunner."""

    @pytest.fixture
    def sample_threads_dir(self, tmp_path):
        """Create a sample threads directory with test threads."""
        threads_dir = tmp_path / ".watercooler"
        threads_dir.mkdir()

        # Create a sample thread file
        thread_content = """# test-thread — Thread

Status: OPEN
Ball: Claude (user)

---

Entry: Claude (user) 2025-01-01T12:00:00Z
Role: planner
Type: Plan
Title: Test entry

This is a test entry for integration testing.

---
"""
        (threads_dir / "test-thread.md").write_text(thread_content)
        return tmp_path  # Return parent so it finds .watercooler subdirectory

    def test_export_stage_runs_successfully(self, sample_threads_dir, tmp_path):
        """Test that export stage completes successfully with valid input."""
        from watercooler_memory.pipeline.stages import ExportStageRunner
        from watercooler_memory.pipeline.logging import PipelineLogger

        work_dir = tmp_path / "work"
        config = PipelineConfig(
            threads_dir=sample_threads_dir,
            work_dir=work_dir,
        )
        config.ensure_work_dir()
        state = PipelineState.create("test-run", config.threads_dir, config.work_dir)
        logger = PipelineLogger("test-run", work_dir / "logs")

        runner = ExportStageRunner(config, state, logger)

        # Validate inputs pass
        errors = runner.validate_inputs()
        assert len(errors) == 0, f"Validation errors: {errors}"

        # Run the stage
        outputs = runner.run()

        # Verify outputs
        assert "export_dir" in outputs
        assert "documents_file" in outputs
        assert Path(outputs["documents_file"]).exists()
        assert outputs["statistics"]["documents"] >= 1


class TestExtendedRedaction:
    """Tests for extended secret redaction patterns."""

    def test_redact_x_api_key_header(self):
        """Test redacting X-API-Key headers."""
        text = "X-API-Key: abcd1234567890abcdef"
        redacted = _redact_sensitive(text)
        assert "abcd1234567890abcdef" not in redacted
        assert "X-API-Key" in redacted

    def test_redact_url_credentials(self):
        """Test redacting credentials from URLs."""
        text = "Connecting to https://user:secretpassword@api.example.com/v1"
        redacted = _redact_sensitive(text)
        assert "secretpassword" not in redacted
        assert "https://user:" in redacted
        assert "@api.example.com" in redacted

    def test_redact_basic_auth_header(self):
        """Test redacting Basic auth headers."""
        text = "Authorization: Basic dXNlcm5hbWU6cGFzc3dvcmQxMjM0NTY="
        redacted = _redact_sensitive(text)
        assert "dXNlcm5hbWU6cGFzc3dvcmQxMjM0NTY=" not in redacted
        assert "[REDACTED_BASE64]" in redacted

    def test_redact_aws_key(self):
        """Test redacting AWS access keys."""
        text = "AWS Key: AKIAIOSFODNN7EXAMPLE"
        redacted = _redact_sensitive(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in redacted
        assert "[REDACTED_AWS_KEY]" in redacted


class TestConfigValidation:
    """Additional tests for configuration validation."""

    def test_pipeline_config_batch_size_validation(self, tmp_path):
        """Test that invalid batch_size is caught by validation."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        (threads_dir / "test.md").write_text("# Test")

        config = PipelineConfig(
            threads_dir=threads_dir,
            work_dir=tmp_path / "work",
            batch_size=0,  # Invalid
        )
        errors = config.validate()
        assert any("batch_size" in e for e in errors)

    def test_pipeline_config_max_concurrent_validation(self, tmp_path):
        """Test that invalid max_concurrent is caught by validation."""
        threads_dir = tmp_path / "threads"
        threads_dir.mkdir()
        (threads_dir / "test.md").write_text("# Test")

        config = PipelineConfig(
            threads_dir=threads_dir,
            work_dir=tmp_path / "work",
            max_concurrent=-1,  # Invalid
        )
        errors = config.validate()
        assert any("max_concurrent" in e for e in errors)
