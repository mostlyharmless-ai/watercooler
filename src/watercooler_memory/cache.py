"""Disk caching for expensive API operations.

Caches summaries and embeddings to disk so they survive pipeline failures.
Results are stored in ~/.watercooler/cache/ by default.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Optional


# Default cache location
DEFAULT_CACHE_DIR = Path.home() / ".watercooler" / "cache"


def _get_cache_dir() -> Path:
    """Get cache directory from environment or default."""
    cache_dir = os.environ.get("WATERCOOLER_CACHE_DIR")
    if cache_dir:
        return Path(cache_dir)
    return DEFAULT_CACHE_DIR


def _content_hash(content: str, prefix: str = "") -> str:
    """Generate a hash key for content."""
    h = hashlib.sha256((prefix + content).encode()).hexdigest()[:16]
    return h


class SummaryCache:
    """Disk cache for LLM-generated summaries."""

    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = cache_dir or _get_cache_dir() / "summaries"
        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _key_path(self, entry_id: str, body_hash: str) -> Path:
        """Get path for a cache entry."""
        # Use entry_id if available, otherwise use body hash
        key = entry_id if entry_id else body_hash
        # Sanitize the key for filesystem
        safe_key = "".join(c if c.isalnum() or c in "-_" else "_" for c in key)
        return self.cache_dir / f"{safe_key}.json"

    def get(self, entry_id: str, body: str) -> Optional[str]:
        """Get cached summary if it exists.

        Args:
            entry_id: Unique identifier for the entry.
            body: Entry body (used for hash if entry_id changes).

        Returns:
            Cached summary or None.
        """
        body_hash = _content_hash(body)
        cache_path = self._key_path(entry_id, body_hash)

        if cache_path.exists():
            try:
                data = json.loads(cache_path.read_text())
                # Verify the content hash matches (in case entry_id was reused)
                if data.get("body_hash") == body_hash:
                    return data.get("summary")
            except (json.JSONDecodeError, KeyError):
                pass

        return None

    def set(self, entry_id: str, body: str, summary: str) -> None:
        """Save summary to cache.

        Args:
            entry_id: Unique identifier for the entry.
            body: Entry body.
            summary: Generated summary.
        """
        body_hash = _content_hash(body)
        cache_path = self._key_path(entry_id, body_hash)

        data = {
            "entry_id": entry_id,
            "body_hash": body_hash,
            "summary": summary,
        }

        cache_path.write_text(json.dumps(data, indent=2))

    def stats(self) -> dict:
        """Return cache statistics."""
        files = list(self.cache_dir.glob("*.json"))
        return {
            "count": len(files),
            "size_bytes": sum(f.stat().st_size for f in files),
            "path": str(self.cache_dir),
        }


class EmbeddingCache:
    """Disk cache for embeddings."""

    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = cache_dir or _get_cache_dir() / "embeddings"
        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _key_path(self, text_hash: str) -> Path:
        """Get path for a cache entry."""
        return self.cache_dir / f"{text_hash}.json"

    def get(self, text: str) -> Optional[list[float]]:
        """Get cached embedding if it exists.

        Args:
            text: Text that was embedded.

        Returns:
            Cached embedding vector or None.
        """
        text_hash = _content_hash(text, prefix="emb:")
        cache_path = self._key_path(text_hash)

        if cache_path.exists():
            try:
                data = json.loads(cache_path.read_text())
                return data.get("embedding")
            except (json.JSONDecodeError, KeyError):
                pass

        return None

    def get_batch(self, texts: list[str]) -> tuple[list[Optional[list[float]]], list[int]]:
        """Get cached embeddings for a batch of texts.

        Args:
            texts: List of texts.

        Returns:
            Tuple of (cached_results, missing_indices).
            cached_results[i] is the embedding or None.
            missing_indices lists indices that need API calls.
        """
        results: list[Optional[list[float]]] = []
        missing: list[int] = []

        for i, text in enumerate(texts):
            cached = self.get(text)
            results.append(cached)
            if cached is None:
                missing.append(i)

        return results, missing

    def set(self, text: str, embedding: list[float]) -> None:
        """Save embedding to cache.

        Args:
            text: Text that was embedded.
            embedding: Embedding vector.
        """
        text_hash = _content_hash(text, prefix="emb:")
        cache_path = self._key_path(text_hash)

        data = {
            "text_hash": text_hash,
            "text_preview": text[:100],  # Store preview for debugging
            "embedding": embedding,
            "dimension": len(embedding),
        }

        cache_path.write_text(json.dumps(data))

    def set_batch(self, texts: list[str], embeddings: list[list[float]]) -> None:
        """Save multiple embeddings to cache.

        Args:
            texts: List of texts.
            embeddings: List of embedding vectors.
        """
        for text, embedding in zip(texts, embeddings):
            self.set(text, embedding)

    def stats(self) -> dict:
        """Return cache statistics."""
        files = list(self.cache_dir.glob("*.json"))
        return {
            "count": len(files),
            "size_bytes": sum(f.stat().st_size for f in files),
            "path": str(self.cache_dir),
        }


class ThreadSummaryCache:
    """Disk cache for thread-level summaries."""

    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = cache_dir or _get_cache_dir() / "thread_summaries"
        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _key_path(self, thread_id: str) -> Path:
        """Get path for a cache entry."""
        safe_key = "".join(c if c.isalnum() or c in "-_" else "_" for c in thread_id)
        return self.cache_dir / f"{safe_key}.json"

    def get(self, thread_id: str, entry_count: int) -> Optional[str]:
        """Get cached thread summary if it exists and is current.

        Args:
            thread_id: Thread identifier.
            entry_count: Current number of entries (invalidates if different).

        Returns:
            Cached summary or None.
        """
        cache_path = self._key_path(thread_id)

        if cache_path.exists():
            try:
                data = json.loads(cache_path.read_text())
                # Invalidate if entry count changed (thread was updated)
                if data.get("entry_count") == entry_count:
                    return data.get("summary")
            except (json.JSONDecodeError, KeyError):
                pass

        return None

    def set(self, thread_id: str, entry_count: int, summary: str) -> None:
        """Save thread summary to cache.

        Args:
            thread_id: Thread identifier.
            entry_count: Number of entries at time of summarization.
            summary: Generated summary.
        """
        cache_path = self._key_path(thread_id)

        data = {
            "thread_id": thread_id,
            "entry_count": entry_count,
            "summary": summary,
        }

        cache_path.write_text(json.dumps(data, indent=2))

    def stats(self) -> dict:
        """Return cache statistics."""
        files = list(self.cache_dir.glob("*.json"))
        return {
            "count": len(files),
            "size_bytes": sum(f.stat().st_size for f in files),
            "path": str(self.cache_dir),
        }


