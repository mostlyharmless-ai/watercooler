"""Tests for the embedding models registry."""

import pytest

from watercooler.embedding_models import (
    DEFAULT_MODEL,
    EMBEDDING_MODELS,
    ModelNotFoundError,
    get_model_dimension,
    get_model_path,
    get_models_dir,
    is_ollama_model,
    resolve_embedding_model,
)


class TestResolveEmbeddingModel:
    """Test resolve_embedding_model function."""

    def test_resolve_known_model(self):
        """Test resolving a known model name."""
        spec = resolve_embedding_model("bge-m3")
        assert spec["hf_repo"] == "KimChen/bge-m3-GGUF"
        assert spec["hf_file"] == "bge-m3-q8_0.gguf"
        assert spec["dim"] == 1024
        assert spec["context"] == 8192

    def test_resolve_nomic_model(self):
        """Test resolving nomic-embed-text model."""
        spec = resolve_embedding_model("nomic-embed-text")
        assert spec["hf_repo"] == "nomic-ai/nomic-embed-text-v1.5-GGUF"
        assert spec["dim"] == 768

    def test_resolve_alias(self):
        """Test resolving an alias."""
        spec1 = resolve_embedding_model("bge-m3")
        spec2 = resolve_embedding_model("bge-m3:latest")
        assert spec1 == spec2

    def test_resolve_case_insensitive(self):
        """Test that resolution is case-insensitive."""
        spec1 = resolve_embedding_model("bge-m3")
        spec2 = resolve_embedding_model("BGE-M3")
        assert spec1 == spec2

    def test_resolve_with_whitespace(self):
        """Test that whitespace is stripped."""
        spec1 = resolve_embedding_model("bge-m3")
        spec2 = resolve_embedding_model("  bge-m3  ")
        assert spec1 == spec2

    def test_resolve_unknown_raises(self):
        """Test that unknown model raises ModelNotFoundError."""
        with pytest.raises(ModelNotFoundError) as exc_info:
            resolve_embedding_model("unknown-model")
        assert "unknown-model" in str(exc_info.value).lower()
        assert "known models" in str(exc_info.value).lower()

    def test_resolve_unknown_with_version_suffix(self):
        """Test that unknown model with version suffix raises helpful error."""
        with pytest.raises(ModelNotFoundError):
            resolve_embedding_model("unknown-model:v1")


class TestGetModelDimension:
    """Test get_model_dimension function."""

    def test_bge_m3_dimension(self):
        """Test bge-m3 returns 1024."""
        assert get_model_dimension("bge-m3") == 1024

    def test_nomic_dimension(self):
        """Test nomic-embed-text returns 768."""
        assert get_model_dimension("nomic-embed-text") == 768

    def test_e5_dimension(self):
        """Test e5-mistral-7b returns 4096."""
        assert get_model_dimension("e5-mistral-7b") == 4096

    def test_unknown_raises(self):
        """Test unknown model raises error."""
        with pytest.raises(ModelNotFoundError):
            get_model_dimension("unknown-model")


class TestGetModelsDir:
    """Test get_models_dir function."""

    def test_returns_path(self):
        """Test that it returns a Path object."""
        from pathlib import Path

        models_dir = get_models_dir()
        assert isinstance(models_dir, Path)
        assert "watercooler" in str(models_dir)
        assert "models" in str(models_dir)


class TestGetModelPath:
    """Test get_model_path function."""

    def test_unknown_model_returns_none(self):
        """Test that unknown model returns None."""
        path = get_model_path("unknown-model")
        assert path is None

    def test_known_model_uncached_returns_none(self):
        """Test that uncached model returns None."""
        # This assumes the model is not already downloaded
        # If it is, this test may need adjustment
        # We check with a model unlikely to be cached
        path = get_model_path("e5-mistral-7b")
        # Could be None (not cached) or Path (cached) - both are valid
        assert path is None or path.exists()


class TestIsOllamaModel:
    """Test is_ollama_model function."""

    def test_known_llama_cpp_models_not_ollama(self):
        """Test that known llama.cpp models are not detected as Ollama."""
        assert is_ollama_model("bge-m3") is False
        assert is_ollama_model("nomic-embed-text") is False
        assert is_ollama_model("e5-mistral-7b") is False

    def test_nomic_pattern_detected(self):
        """Test that nomic patterns in unknown models are detected."""
        # Unknown model with nomic pattern
        assert is_ollama_model("nomic-embed-text-custom") is True

    def test_other_ollama_patterns(self):
        """Test other Ollama model patterns."""
        assert is_ollama_model("all-minilm") is True
        assert is_ollama_model("mxbai-embed-large") is True


class TestEmbeddingModelsRegistry:
    """Test EMBEDDING_MODELS registry structure."""

    def test_default_model_exists(self):
        """Test that DEFAULT_MODEL is in the registry."""
        assert DEFAULT_MODEL in EMBEDDING_MODELS

    def test_all_aliases_resolve(self):
        """Test that all aliases in registry resolve to valid specs."""
        for name, value in EMBEDDING_MODELS.items():
            if isinstance(value, str):
                # It's an alias, verify target exists
                assert value in EMBEDDING_MODELS

    def test_all_specs_have_required_fields(self):
        """Test that all specs have required fields."""
        for name, value in EMBEDDING_MODELS.items():
            if isinstance(value, dict):
                assert "hf_repo" in value
                assert "hf_file" in value
                assert "dim" in value
